"""analyze.py 회귀 테스트 — 구간 전개·orchestration·통합 보고서.

resolve_sha.py·predecessors.py 자체 판정은 각자의 테스트가 검증한다.
여기서는 구간 전개 규칙(양끝 포함·ancestor 검증·상한), 자식 실행·오류
전파, 통합 stdout JSON·통합 보고서 구성만 본다.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "analyze.py"

H1, H2 = "1" * 40, "2" * 40


def g(repo: Path, *args: str, env: dict | None = None) -> str:
    p = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
         "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True,
        env={**os.environ, **env} if env else None)
    assert p.returncode == 0, f"git {args} 실패: {p.stderr}"
    return p.stdout.strip()


def commit_file(repo: Path, name: str, content: str, msg: str) -> str:
    (repo / name).write_text(content)
    g(repo, "add", ".")
    g(repo, "commit", "-q", "-m", msg)
    return g(repo, "rev-parse", "HEAD")


class AnalyzeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        base = Path(cls._tmp.name)
        cls.ftl, cls.integ = base / "ftl", base / "integ"
        cls.ftl.mkdir()
        cls.integ.mkdir()

        g(cls.ftl, "init", "-q", "-b", "main")
        cls.b1 = commit_file(cls.ftl, "base.txt", "base\n", "base")
        cls.c1 = commit_file(cls.ftl, "a.txt", "a1\n", "AGCD-1: add a")
        cls.c2 = commit_file(cls.ftl, "b.txt", "b1\n", "AGCD-2: add b")
        cls.c3 = commit_file(cls.ftl, "a.txt", "a1\na3\n", "AGCD-3: tweak a")
        cls.c4 = commit_file(cls.ftl, "a.txt", "a1\na3\na4\n", "AGCD-4: finish a")

        g(cls.ftl, "checkout", "-q", "-b", "develop", cls.b1)
        g(cls.ftl, "cherry-pick", cls.c1,
          env={"GIT_COMMITTER_DATE": "2030-01-02T03:04:05 +0000"})
        g(cls.ftl, "checkout", "-q", "main")

        g(cls.integ, "init", "-q", "-b", "main")

        def peg(msg: str, **links: str) -> str:
            for path, sha in links.items():
                g(cls.integ, "update-index", "--add",
                  "--cacheinfo", f"160000,{sha},{path}")
            g(cls.integ, "commit", "-q", "-m", msg)
            return g(cls.integ, "rev-parse", "HEAD")

        cls.p1 = peg("peg: baseline",
                     **{"Src/FTL": cls.b1, "Src/HAL": H1})
        cls.p2 = peg("peg: FTL c1-c2 + HAL",
                     **{"Src/FTL": cls.c2, "Src/HAL": H2})
        cls.p3 = peg("peg: FTL c3-c4", **{"Src/FTL": cls.c4})

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    @classmethod
    def run_tool(cls, *args: str, expect_code: int = 0) -> dict:
        p = subprocess.run(
            [sys.executable, str(SCRIPT),
             "--repo", str(cls.integ), "--source-branch", "main",
             "--submodule", "Src/FTL", "--ftl-repo", str(cls.ftl),
             "--target-branch", "develop", *args],
            capture_output=True, text=True)
        assert p.returncode == expect_code, \
            f"exit {p.returncode} != {expect_code}: {p.stdout} {p.stderr}"
        return json.loads(p.stdout)

    # ------------------------------------------------------------ 통합 흐름

    def test_range_runs_both_scripts_and_writes_combined_report(self):
        report = Path(self._tmp.name) / "combined.html"
        out = self.run_tool(self.c2, self.c4, "--html", str(report))
        self.assertEqual(out["mode"], "analyze")
        # 구간 전개 — 양끝 포함, 오래된 순
        self.assertEqual(out["range"]["from"], self.c2)
        self.assertEqual(out["range"]["to"], self.c4)
        self.assertEqual(out["range"]["commits_total"], 3)
        self.assertEqual([q["input"] for q in out["predecessors"]["queries"]],
                         [self.c2, self.c3, self.c4])
        # resolve 결과 — 구간이 P2·P3 두 pegging에 걸침
        self.assertEqual([b["pegging"]["sha"] for b in
                          out["resolve"]["peggings"]], [self.p2, self.p3])
        # 그래프는 stdout에 싣지 않는다 (보고서 전용)
        self.assertNotIn("graph", out["predecessors"])
        self.assertNotIn(str(report), json.dumps(out))

        html = report.read_text(encoding="utf-8")
        m = re.search(
            r'<script id="data" type="application/json">(.*?)</script>',
            html, re.S)
        data = json.loads(m.group(1))
        # 통합 보고서 — predecessors 데이터 + resolve 섹션 + 구간 + 그래프
        self.assertEqual(len(data["queries"]), 3)
        self.assertEqual([b["pegging"]["sha"] for b in
                          data["resolve"]["peggings"]], [self.p2, self.p3])
        self.assertEqual(data["range"]["commits_total"], 3)
        self.assertTrue(data["graph"]["nodes"])
        self.assertNotIn("t@t", html)

    def test_single_commit_range(self):
        out = self.run_tool(self.c3, self.c3)
        self.assertEqual(out["range"]["commits_total"], 1)
        self.assertEqual([q["input"] for q in out["predecessors"]["queries"]],
                         [self.c3])

    # ------------------------------------------------------------ 오류

    def test_reversed_range_is_rejected(self):
        out = self.run_tool(self.c4, self.c2, expect_code=2)
        self.assertEqual(out["error_code"], "INVALID_RANGE")

    def test_range_too_large(self):
        out = self.run_tool(self.c1, self.c4, "--max-range", "2",
                            expect_code=2)
        self.assertEqual(out["error_code"], "RANGE_TOO_LARGE")

    def test_child_failure_propagates(self):
        out = self.run_tool(self.c2, self.c4, "--target-branch", "no-such-branch",
                            expect_code=2)
        self.assertFalse(out["ok"])
        self.assertEqual(out["stage"], "predecessors")
        self.assertEqual(out["error_code"], "TARGET_NOT_FOUND")

    def test_help_requires_confirmed_branches(self):
        p = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            capture_output=True, text=True)
        self.assertEqual(p.returncode, 0)
        self.assertIn("--target-branch", p.stdout)
        self.assertIn("추측 금지", p.stdout)
        self.assertIn("FROM", p.stdout)

    def test_since_passes_through_to_predecessors(self):
        out = self.run_tool(self.c2, self.c4, "--since", "2000-01-01")
        self.assertEqual(out["predecessors"]["window"],
                         {"since": "2000-01-01", "excluded_total": 0})

    def test_since_window_reaches_combined_report(self):
        report = Path(self._tmp.name) / "window.html"
        self.run_tool(self.c2, self.c4, "--since", "2000-01-01",
                      "--html", str(report))
        m = re.search(
            r'<script id="data" type="application/json">(.*?)</script>',
            report.read_text(encoding="utf-8"), re.S)
        data = json.loads(m.group(1))
        self.assertEqual(data["window"],
                         {"since": "2000-01-01", "excluded_total": 0})

    # ------------------------------------------------------------ --output

    def test_output_writes_full_json_stdout_gets_summary(self):
        """--output: 자식 전체 출력은 파일, stdout은 두 자식의 요약."""
        path = Path(self._tmp.name) / "analyze_out.json"
        out = self.run_tool(self.c2, self.c4, "--output", str(path))
        self.assertTrue(out["output_written"])
        self.assertEqual(out["range"]["commits_total"], 3)
        self.assertIn("summary", out["resolve"])
        self.assertIn("summary", out["predecessors"])
        self.assertNotIn("peggings", out["resolve"])  # 상세는 파일에만
        self.assertTrue(all("predecessors" not in q
                            for q in out["predecessors"]["queries"]))
        self.assertNotIn(str(path), json.dumps(out))  # 로컬 경로 금지 정책

        full = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(full["schema_version"], 1)
        self.assertEqual(full["mode"], "analyze")
        self.assertTrue(full["resolve"]["peggings"])
        self.assertTrue(
            any(q["predecessors"] for q in full["predecessors"]["queries"]))

    def test_output_unwritable_path(self):
        out = self.run_tool(
            self.c2, self.c4, "--output",
            str(Path(self._tmp.name) / "no-such-dir" / "out.json"),
            expect_code=3)
        self.assertEqual(out["error_code"], "OUTPUT_WRITE_FAILED")


if __name__ == "__main__":
    unittest.main()
