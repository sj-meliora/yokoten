"""resolve_sha.py 회귀 테스트.

실제 git repo 픽스처를 만들어 CLI를 subprocess로 검증한다. integration의
pegging 커밋은 `update-index --cacheinfo 160000`으로 gitlink만 스테이징해
만든다 — 실제 submodule 초기화 없이 gitlink 트리를 구성하는 표준 기법.

픽스처 시나리오:

  FTL:    f1 → f2 → f3 → f4 → f5 → f6
  HAL:    h1 → h2
  Shared: s1

  integration(main):
    P1  FTL=f1, HAL=h1, Shared=s1   (baseline)
    P2  FTL=f3                      (f2·f3 batch — FTL 단독)
    P3  FTL=f4, HAL=h2              (단독 pegging + HAL 동반)

  f5·f6은 미pegging.
"""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "resolve_sha.py"


def g(repo: Path, *args: str) -> str:
    p = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
         "-c", "commit.gpgsign=false", *args],
        capture_output=True, text=True)
    assert p.returncode == 0, f"git {args} 실패: {p.stderr}"
    return p.stdout.strip()


def make_commits(repo: Path, prefix: str, n: int) -> list[str]:
    g(repo, "init", "-q", "-b", "main")
    shas = []
    for i in range(1, n + 1):
        (repo / f"{prefix}{i}.txt").write_text(f"{prefix}{i}\n")
        g(repo, "add", ".")
        g(repo, "commit", "-q", "-m", f"{prefix}: change {i}")
        shas.append(g(repo, "rev-parse", "HEAD"))
    return shas


class ResolveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        base = Path(cls._tmp.name)
        cls.ftl, cls.hal, cls.shared, cls.integ = (
            base / "ftl", base / "hal", base / "shared", base / "integ")
        for d in (cls.ftl, cls.hal, cls.shared, cls.integ):
            d.mkdir()

        cls.f = make_commits(cls.ftl, "f", 6)
        cls.h = make_commits(cls.hal, "h", 2)
        cls.s = make_commits(cls.shared, "s", 1)

        g(cls.integ, "init", "-q", "-b", "main")

        def peg(msg: str, **links: str) -> str:
            for path, sha in links.items():
                g(cls.integ, "update-index", "--add",
                  "--cacheinfo", f"160000,{sha},{path}")
            g(cls.integ, "commit", "-q", "-m", msg)
            return g(cls.integ, "rev-parse", "HEAD")

        cls.p1 = peg("peg: baseline", FTL=cls.f[0], HAL=cls.h[0], Shared=cls.s[0])
        cls.p2 = peg("peg: FTL f2-f3", FTL=cls.f[2])
        cls.p3 = peg("peg: FTL f4 + HAL h2", FTL=cls.f[3], HAL=cls.h[1])

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    @classmethod
    def run_tool(cls, *args: str, expect_code: int = 0) -> dict:
        p = subprocess.run(
            [sys.executable, str(SCRIPT),
             "--repo", str(cls.integ), "--branch", "main",
             "--ftl-repo", str(cls.ftl),
             "--sub-repo", f"HAL={cls.hal}", "--sub-repo", f"Shared={cls.shared}",
             *args],
            capture_output=True, text=True)
        assert p.returncode == expect_code, \
            f"exit {p.returncode} != {expect_code}: {p.stdout} {p.stderr}"
        return json.loads(p.stdout)

    # ------------------------------------------------------------ 판정

    def test_batch_no_companion(self):
        """batch에 묻힌 sha — 이진 탐색으로 경계 pegging, 동반 없음 확정."""
        out = self.run_tool(self.f[1])  # f2
        q = out["queries"][0]
        self.assertEqual(q["status"], "found")
        self.assertEqual(q["search"], "binary")
        self.assertFalse(q["exact_gitlink_match"])
        self.assertEqual(q["pegging"], self.p2[:7])

        (blk,) = out["peggings"]
        self.assertEqual(blk["pegging"]["sha"], self.p2)
        self.assertEqual(blk["prev_pegging"]["sha"], self.p1)
        self.assertEqual(blk["companion_status"], "no_companion")
        self.assertEqual(blk["companions"], [])
        batch = blk["ftl"]["batch"]
        self.assertEqual([c["sha"] for c in batch], [self.f[1], self.f[2]])
        self.assertEqual([c["queried"] for c in batch], [True, False])

    def test_exact_match_pickaxe(self):
        """gitlink 값과 정확히 일치하는 sha — pickaxe fast path."""
        out = self.run_tool(self.f[2])  # f3 == P2의 gitlink
        q = out["queries"][0]
        self.assertEqual(q["status"], "found")
        self.assertEqual(q["search"], "pickaxe")
        self.assertTrue(q["exact_gitlink_match"])
        self.assertEqual(q["pegging"], self.p2[:7])

    def test_coupled(self):
        """단독 pegging + HAL gitlink 동반 이동 — coupled 확정."""
        out = self.run_tool(self.f[3])  # f4
        (blk,) = out["peggings"]
        self.assertEqual(blk["pegging"]["sha"], self.p3)
        self.assertEqual(blk["companion_status"], "coupled")
        self.assertEqual(blk["ftl"]["batch_total"], 1)
        (hal,) = blk["companions"]
        self.assertEqual(hal["path"], "HAL")
        self.assertEqual(hal["from"], self.h[0])
        self.assertEqual(hal["to"], self.h[1])
        self.assertEqual([c["sha"] for c in hal["commits"]], [self.h[1]])
        self.assertTrue(hal["repo_available"])

    def test_baseline_boundary(self):
        """가장 오래된 pegging에 이미 포함 — batch 미계산, root라 동반 판정 불가."""
        out = self.run_tool(self.f[0])  # f1
        q = out["queries"][0]
        self.assertEqual(q["status"], "found")
        self.assertEqual(q["pegging"], self.p1[:7])
        (blk,) = out["peggings"]
        self.assertIsNone(blk["prev_pegging"])
        self.assertIsNone(blk["ftl"]["batch"])
        self.assertEqual(blk["companion_status"], "unknown")

    def test_not_pegged(self):
        out = self.run_tool(self.f[5])  # f6
        q = out["queries"][0]
        self.assertEqual(q["status"], "not_pegged")
        self.assertIsNone(q["pegging"])
        self.assertEqual(out["peggings"], [])

    def test_not_found_in_ftl(self):
        out = self.run_tool("deadbeefdeadbee")
        self.assertEqual(out["queries"][0]["status"], "not_found_in_ftl")

    def test_thorough_agrees_with_binary(self):
        out = self.run_tool(self.f[1], "--thorough")
        q = out["queries"][0]
        self.assertEqual(q["search"], "linear")
        self.assertEqual(q["pegging"], self.p2[:7])

    # ------------------------------------------------------------ 입출력

    def test_grouping_by_pegging(self):
        """여러 sha가 pegging 단위로 묶여 나온다."""
        out = self.run_tool(self.f[1], self.f[2], self.f[3])
        self.assertEqual(len(out["queries"]), 3)
        self.assertEqual([b["pegging"]["sha"] for b in out["peggings"]],
                         [self.p2, self.p3])
        batch = out["peggings"][0]["ftl"]["batch"]
        self.assertEqual([c["queried"] for c in batch], [True, True])

    def test_input_csv(self):
        csv = Path(self._tmp.name) / "picks.csv"
        csv.write_text(f"sha,memo\n{self.f[3]},yokoten\n")
        out = self.run_tool("--input", str(csv))
        self.assertEqual(out["queries"][0]["status"], "found")
        self.assertTrue(any("무시" in n for n in out["notes"]))  # 헤더 줄

    def test_no_author_in_output(self):
        """회사 AI 정책 — 출력에 개발자 식별 정보가 없어야 한다."""
        out = self.run_tool(self.f[3])
        text = json.dumps(out)
        self.assertNotIn("t@t", text)
        self.assertNotIn("author", text.lower())

    def test_invalid_sha_arg(self):
        out = self.run_tool("not-a-sha", expect_code=2)
        self.assertFalse(out["ok"])
        self.assertEqual(out["error_code"], "INVALID_ARGUMENT")

    def test_missing_branch(self):
        p = subprocess.run(
            [sys.executable, str(SCRIPT), "--repo", str(self.integ), self.f[0]],
            capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)
        self.assertEqual(json.loads(p.stdout)["error_code"], "INVALID_ARGUMENT")

    def test_bad_branch(self):
        out = self.run_tool(self.f[0], "--branch", "no-such-branch",
                            expect_code=2)
        self.assertEqual(out["error_code"], "BRANCH_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
