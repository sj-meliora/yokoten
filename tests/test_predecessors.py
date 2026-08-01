"""predecessors.py 회귀 테스트.

test_resolve.py와 같은 방식 — 실제 git repo 픽스처를 만들어 CLI를 subprocess로
검증한다. 횡전개(cherry-pick) 반영 여부 판정을 위해 FTL repo 안에 source
branch(main)와 target branch(develop)를 함께 구성한다.

픽스처 시나리오:

  FTL(main):    b1 → c1(AGCD-1, a.txt) → c2(AGCD-2, b.txt)
                   → c3(AGCD-3, a.txt) → c4(AGCD-4, a.txt) → c5(AGCD-5, b.txt)
  FTL(develop): b1 → pick(c1)                # 깨끗한 pick — patch 등가
                   → d2(AGCD-2, b.txt 변형)  # 변형 pick — patch 다름, key 유지

  integration(main):
    P1  Src/FTL=b1, Src/HAL=h1               (baseline)
    P2  Src/FTL=c2, Src/HAL=h2               (batch c1·c2 + HAL 동반)
    P3  Src/FTL=c4                           (batch c3·c4, FTL 단독)

  c5는 미pegging. HAL gitlink는 object 없이 sha만 기록한다 (gitlink는 부모
  repo odb에 object가 없어도 된다).
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "predecessors.py"

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


class PredecessorsTest(unittest.TestCase):
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
        cls.c5 = commit_file(cls.ftl, "b.txt", "b1\nb5\n", "AGCD-5: tweak b")

        # target branch — c1은 깨끗한 pick(patch 등가), AGCD-2는 변형 반영.
        # committer date를 바꿔 원본과 byte-identical(동일 sha) 커밋이 되는
        # 것을 막는다 — 실제 횡전개 pick은 항상 다른 sha다.
        g(cls.ftl, "checkout", "-q", "-b", "develop", cls.b1)
        g(cls.ftl, "cherry-pick", cls.c1,
          env={"GIT_COMMITTER_DATE": "2030-01-02T03:04:05 +0000"})
        commit_file(cls.ftl, "b.txt", "b1-modified\n", "AGCD-2: add b (yokoten)")
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
             "--repo", str(cls.integ), "--branch", "main",
             "--submodule", "Src/FTL", "--ftl-repo", str(cls.ftl),
             "--target", "develop", *args],
            capture_output=True, text=True)
        assert p.returncode == expect_code, \
            f"exit {p.returncode} != {expect_code}: {p.stdout} {p.stderr}"
        return json.loads(p.stdout)

    # ------------------------------------------------------------ 판정

    def test_predecessors_with_evidence_risk_and_pegging(self):
        """미반영 선행 커밋을 patch 등가·IMS key·파일 겹침·pegging으로 판정."""
        out = self.run_tool(self.c4)
        self.assertEqual(out["mode"], "predecessors")
        self.assertEqual(out["target"]["ref"], "develop")
        q = out["queries"][0]
        self.assertEqual(q["status"], "found")
        self.assertEqual(q["pegging"], self.p3[:7])
        self.assertEqual(q["self"],
                         {"applied": "not_applied", "ims_keys": ["AGCD-4"]})
        # c1은 patch 등가로 반영 완료 → 목록 제외, applied_total로 집계
        self.assertEqual(q["applied_total"], 1)
        self.assertEqual(q["merges_skipped"], 0)
        self.assertFalse(q["predecessors_truncated"])
        self.assertEqual(q["predecessors_total"], 2)

        c2, c3 = q["predecessors"]  # 오래된 순
        self.assertEqual(c2["sha"], self.c2)
        self.assertEqual(c2["pegging"], self.p2[:7])
        self.assertFalse(c2["same_batch"])
        self.assertEqual(c2["ims_keys"], ["AGCD-2"])
        self.assertEqual(c2["applied_evidence"], "ims_key")  # 변형 pick 감지
        self.assertEqual(c2["risk"], "independent")
        self.assertEqual(c2["overlap_paths"], [])
        self.assertEqual(c2["companions_moved"], ["Src/HAL"])

        self.assertEqual(c3["sha"], self.c3)
        self.assertEqual(c3["pegging"], self.p3[:7])
        self.assertTrue(c3["same_batch"])
        self.assertEqual(c3["applied_evidence"], "none")
        self.assertEqual(c3["risk"], "required_first")
        self.assertEqual(c3["overlap_paths"], ["a.txt"])
        self.assertEqual(c3["companions_moved"], [])

    def test_self_patch_applied(self):
        """깨끗하게 pick된 sha — patch 등가로 이미 반영 완료 판정."""
        out = self.run_tool(self.c1)
        q = out["queries"][0]
        self.assertEqual(q["self"]["applied"], "patch_applied")
        self.assertEqual(q["predecessors"], [])
        self.assertEqual(q["applied_total"], 0)

    def test_self_key_matched(self):
        """변형 pick된 sha — patch는 다르지만 IMS key로 반영 흔적 감지."""
        out = self.run_tool(self.c2)
        q = out["queries"][0]
        self.assertEqual(q["self"]["applied"], "key_matched")
        self.assertEqual(q["self"]["ims_keys"], ["AGCD-2"])
        self.assertEqual(q["predecessors"], [])  # c1은 반영 완료
        self.assertEqual(q["applied_total"], 1)

    def test_not_pegged_query_still_scans_ancestry(self):
        """미배달 sha도 ancestry 기준 사전 점검이 가능하다."""
        out = self.run_tool(self.c5)
        q = out["queries"][0]
        self.assertEqual(q["status"], "not_pegged")
        self.assertIsNone(q["pegging"])
        self.assertEqual(q["self"]["applied"], "not_applied")
        preds = {p["sha"]: p for p in q["predecessors"]}
        self.assertEqual(set(preds), {self.c2, self.c3, self.c4})
        # F가 미배달이라 same_batch는 판정 불가(null), pegging은 각자 보고
        self.assertEqual(preds[self.c4]["pegging"], self.p3[:7])
        self.assertIsNone(preds[self.c4]["same_batch"])
        # c5는 b.txt 변경 — b.txt를 만든 c2가 required_first로 승격
        self.assertEqual(preds[self.c2]["risk"], "required_first")
        self.assertEqual(preds[self.c2]["overlap_paths"], ["b.txt"])
        self.assertEqual(preds[self.c3]["risk"], "independent")

    def test_limit_truncates_oldest_first(self):
        out = self.run_tool(self.c4, "--limit", "1")
        q = out["queries"][0]
        self.assertTrue(q["predecessors_truncated"])
        self.assertEqual(q["predecessors_total"], 2)
        self.assertEqual([p["sha"] for p in q["predecessors"]], [self.c2])

    # ------------------------------------------------------------ HTML 리포트

    def test_html_report_embeds_ancestor_graph(self):
        """--html — 구간 그래프(부모 edge + 반영 상태)를 내장한 리포트 생성."""
        report = Path(self._tmp.name) / "report.html"
        out = self.run_tool(self.c4, "--html", str(report))
        # stdout JSON 계약 불변 + 로컬 경로 미노출
        self.assertEqual(out["queries"][0]["status"], "found")
        self.assertNotIn(str(report), json.dumps(out))
        self.assertTrue(any("리포트" in n for n in out["notes"]))

        html = report.read_text(encoding="utf-8")
        self.assertIn("<!doctype html", html.lower())
        self.assertNotIn("http://", html)   # self-contained — 외부 리소스 없음
        self.assertNotIn("https://", html)
        m = re.search(
            r'<script id="data" type="application/json">(.*?)</script>',
            html, re.S)
        self.assertIsNotNone(m)
        data = json.loads(m.group(1))  # "<\/"는 JSON 표준 escape — 그대로 파싱
        self.assertEqual(data["target"]["ref"], "develop")

        graph = data["graphs"][self.c4]
        self.assertFalse(graph["truncated"])
        nodes = {n["sha"]: n for n in graph["nodes"]}
        # 조상 드릴다운 판정: patch 등가 / key 일치 / 미반영
        self.assertEqual(nodes[self.c1]["status"], "patch_applied")
        self.assertEqual(nodes[self.c2]["status"], "key_matched")
        self.assertEqual(nodes[self.c3]["status"], "not_applied")
        self.assertEqual(nodes[self.c4]["status"], "not_applied")
        # 부모 edge로 조상 탐색 가능, target 이력(b1)은 경계 밖
        self.assertEqual(nodes[self.c4]["parents"], [self.c3])
        self.assertNotIn(self.b1, nodes)
        self.assertEqual(nodes[self.c1]["parents"], [self.b1])
        # 회사 AI 정책 — 리포트에도 개발자 식별 정보 없음
        self.assertNotIn("t@t", html)

    def test_html_report_write_failure(self):
        bad = Path(self._tmp.name) / "no-such-dir" / "report.html"
        out = self.run_tool(self.c4, "--html", str(bad), expect_code=3)
        self.assertEqual(out["error_code"], "REPORT_WRITE_FAILED")

    # ------------------------------------------------------------ 입출력

    def test_not_found_in_ftl(self):
        out = self.run_tool("deadbeefdeadbee")
        q = out["queries"][0]
        self.assertEqual(q["status"], "not_found_in_ftl")
        self.assertIsNone(q["predecessors"])

    def test_target_not_found(self):
        out = self.run_tool(self.c4, "--target", "no-such-branch",
                            expect_code=2)
        self.assertEqual(out["error_code"], "TARGET_NOT_FOUND")

    def test_bad_ims_pattern(self):
        out = self.run_tool(self.c4, "--ims-pattern", "(", expect_code=2)
        self.assertEqual(out["error_code"], "INVALID_ARGUMENT")

    def test_no_author_in_output(self):
        """회사 AI 정책 — 출력에 개발자 식별 정보가 없어야 한다."""
        out = self.run_tool(self.c4)
        text = json.dumps(out)
        self.assertNotIn("t@t", text)
        self.assertNotIn("author", text.lower())

    def test_help_requires_confirmed_branches(self):
        p = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            capture_output=True, text=True)
        self.assertEqual(p.returncode, 0)
        self.assertIn("--target", p.stdout)
        self.assertIn("origin/develop", p.stdout)
        self.assertIn("추측 금지", p.stdout)
        self.assertIn("사용자에게", p.stdout)


if __name__ == "__main__":
    unittest.main()
