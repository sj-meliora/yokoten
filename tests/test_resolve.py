"""resolve_sha.py 회귀 테스트.

실제 git repo 픽스처를 만들어 CLI를 subprocess로 검증한다. integration의
pegging 커밋은 `update-index --cacheinfo 160000`으로 gitlink만 스테이징해
만든다 — 실제 submodule 초기화 없이 gitlink 트리를 구성하는 표준 기법.

픽스처 시나리오:

  FTL:    f1 → f2 → f3 → f4 → f5 → f6
  HAL:    h1 → h2
  Shared: s1 → s2
  FIL:    i1 → i2

  integration(main):
    P1  Src/FTL=f1, Src/HAL=h1, Src/Shared=s1, Src/FIL=i1   (baseline)
    P2  Src/FTL=f3                                          (FTL 단독 batch)
    P3  Src/FTL=f4, Src/HAL=h2, Src/Shared=s2, Src/FIL=i2   (동반)

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
        cls.ftl, cls.hal, cls.shared, cls.fil, cls.integ = (
            base / "ftl", base / "hal", base / "shared", base / "fil",
            base / "integ")
        for d in (cls.ftl, cls.hal, cls.shared, cls.fil, cls.integ):
            d.mkdir()

        cls.f = make_commits(cls.ftl, "f", 6)
        cls.h = make_commits(cls.hal, "h", 2)
        cls.s = make_commits(cls.shared, "s", 2)
        cls.i = make_commits(cls.fil, "i", 2)

        g(cls.integ, "init", "-q", "-b", "main")

        def peg(msg: str, **links: str) -> str:
            for path, sha in links.items():
                g(cls.integ, "update-index", "--add",
                  "--cacheinfo", f"160000,{sha},{path}")
            g(cls.integ, "commit", "-q", "-m", msg)
            return g(cls.integ, "rev-parse", "HEAD")

        cls.p1 = peg("peg: baseline", **{
            "Src/FTL": cls.f[0], "Src/HAL": cls.h[0],
            "Src/Shared": cls.s[0], "Src/FIL": cls.i[0]})
        cls.p2 = peg("peg: FTL f2-f3", **{"Src/FTL": cls.f[2]})
        cls.p3 = peg("peg: FTL f4 + HAL h2 + Shared s2 + FIL i2", **{
            "Src/FTL": cls.f[3], "Src/HAL": cls.h[1],
            "Src/Shared": cls.s[1], "Src/FIL": cls.i[1]})

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    @classmethod
    def run_tool(cls, *args: str, expect_code: int = 0,
                 with_sub_repos: bool = True) -> dict:
        command = [sys.executable, str(SCRIPT),
                   "--repo", str(cls.integ), "--source-branch", "main",
                   "--submodule", "Src/FTL", "--ftl-repo", str(cls.ftl)]
        if with_sub_repos:
            command.extend(("--sub-repo", f"Src/HAL={cls.hal}",
                            "--sub-repo", f"Src/Shared={cls.shared}",
                            "--sub-repo", f"Src/FIL={cls.fil}"))
        p = subprocess.run(
            [*command, *args],
            capture_output=True, text=True)
        assert p.returncode == expect_code, \
            f"exit {p.returncode} != {expect_code}: {p.stdout} {p.stderr}"
        return json.loads(p.stdout)

    # ------------------------------------------------------------ 판정

    def test_fixture_has_gitlinks_without_submodule_checkout(self):
        """픽스처가 checkout 없이 실제 160000 gitlink 트리를 구성한다."""
        paths = {"Src/FTL", "Src/HAL", "Src/Shared", "Src/FIL"}
        tree = g(self.integ, "ls-tree", self.p3, "--", *paths)
        entries = {line.split("\t", 1)[1]: line.split(None, 3)[:3]
                   for line in tree.splitlines()}
        self.assertEqual(set(entries), paths)
        self.assertTrue(all(mode == "160000" and kind == "commit"
                            for mode, kind, _ in entries.values()))
        for path in paths:
            self.assertFalse((self.integ / path).exists())

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

    def test_limit_keeps_most_recent(self):
        """--limit 절단은 오래된 쪽을 잘라내고 최근 커밋을 남긴다."""
        out = self.run_tool(self.f[1], "--limit", "1")
        ftl = out["peggings"][0]["ftl"]
        self.assertTrue(ftl["batch_truncated"])
        self.assertEqual(ftl["batch_total"], 2)
        self.assertEqual([c["sha"] for c in ftl["batch"]], [self.f[2]])

    def test_exact_match_pickaxe(self):
        """gitlink 값과 정확히 일치하는 sha — pickaxe fast path."""
        out = self.run_tool(self.f[2])  # f3 == P2의 gitlink
        q = out["queries"][0]
        self.assertEqual(q["status"], "found")
        self.assertEqual(q["search"], "pickaxe")
        self.assertTrue(q["exact_gitlink_match"])
        self.assertEqual(q["pegging"], self.p2[:7])

    def test_coupled(self):
        """단독 pegging + HAL/Shared/FIL gitlink 동반 이동 — coupled 확정."""
        out = self.run_tool(self.f[3])  # f4
        (blk,) = out["peggings"]
        self.assertEqual(blk["pegging"]["sha"], self.p3)
        self.assertEqual(blk["companion_status"], "coupled")
        self.assertEqual(blk["ftl"]["batch_total"], 1)
        companions = {item["path"]: item for item in blk["companions"]}
        self.assertEqual(set(companions), {"Src/HAL", "Src/Shared", "Src/FIL"})
        hal = companions["Src/HAL"]
        self.assertEqual(hal["from"], self.h[0])
        self.assertEqual(hal["to"], self.h[1])
        self.assertEqual([c["sha"] for c in hal["commits"]], [self.h[1]])
        self.assertTrue(hal["repo_available"])

        shared = companions["Src/Shared"]
        self.assertEqual(shared["from"], self.s[0])
        self.assertEqual(shared["to"], self.s[1])
        self.assertEqual([c["sha"] for c in shared["commits"]], [self.s[1]])
        self.assertTrue(shared["repo_available"])

        fil = companions["Src/FIL"]
        self.assertEqual(fil["from"], self.i[0])
        self.assertEqual(fil["to"], self.i[1])
        self.assertEqual([c["sha"] for c in fil["commits"]], [self.i[1]])
        self.assertTrue(fil["repo_available"])

    def test_gitlink_change_is_reported_without_companion_checkout(self):
        """HAL/Shared/FIL clone 없이도 tree만으로 동반 이동을 식별한다."""
        out = self.run_tool(self.f[3], with_sub_repos=False)
        companions = {item["path"]: item
                      for item in out["peggings"][0]["companions"]}
        self.assertEqual(set(companions), {"Src/HAL", "Src/Shared", "Src/FIL"})
        hal = companions["Src/HAL"]
        self.assertEqual((hal["from"], hal["to"]), (self.h[0], self.h[1]))
        shared = companions["Src/Shared"]
        self.assertEqual((shared["from"], shared["to"]),
                         (self.s[0], self.s[1]))
        fil = companions["Src/FIL"]
        self.assertEqual((fil["from"], fil["to"]), (self.i[0], self.i[1]))
        for companion in companions.values():
            self.assertFalse(companion["repo_available"])
            self.assertIsNone(companion["commits"])
        notes = out["peggings"][0]["notes"]
        for path in companions:
            self.assertTrue(any(f"'{path}' repo 접근 불가" in note
                                for note in notes))

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

    def test_fetch_refreshes_ftl_and_integration_before_verdict(self):
        """FTL만 최신인 비대칭 snapshot 대신 두 remote ref를 먼저 갱신한다."""
        base = Path(self._tmp.name) / "fetch-sync"
        base.mkdir()
        ftl_seed, integ_seed = base / "ftl-seed", base / "integ-seed"
        ftl_remote, integ_remote = base / "ftl.git", base / "integ.git"
        ftl_clone, integ_clone = base / "ftl-clone", base / "integ-clone"
        for repo in (ftl_seed, integ_seed, ftl_remote, integ_remote):
            repo.mkdir()

        f1 = make_commits(ftl_seed, "sync", 1)[0]
        g(integ_seed, "init", "-q", "-b", "main")
        g(integ_seed, "update-index", "--add", "--cacheinfo",
          f"160000,{f1},Src/FTL")
        g(integ_seed, "commit", "-q", "-m", "peg f1")
        g(ftl_remote, "init", "-q", "--bare")
        g(integ_remote, "init", "-q", "--bare")
        g(ftl_seed, "remote", "add", "origin", str(ftl_remote))
        g(integ_seed, "remote", "add", "origin", str(integ_remote))
        g(ftl_seed, "push", "-q", "-u", "origin", "main")
        g(integ_seed, "push", "-q", "-u", "origin", "main")
        g(ftl_remote, "symbolic-ref", "HEAD", "refs/heads/main")
        g(integ_remote, "symbolic-ref", "HEAD", "refs/heads/main")
        subprocess.run(["git", "clone", "-q", str(ftl_remote), str(ftl_clone)],
                       check=True)
        subprocess.run(["git", "clone", "-q", str(integ_remote), str(integ_clone)],
                       check=True)

        (ftl_seed / "sync2.txt").write_text("sync2\n")
        g(ftl_seed, "add", ".")
        g(ftl_seed, "commit", "-q", "-m", "sync: change 2")
        f2 = g(ftl_seed, "rev-parse", "HEAD")
        g(ftl_seed, "push", "-q")
        g(integ_seed, "update-index", "--cacheinfo", f"160000,{f2},Src/FTL")
        g(integ_seed, "commit", "-q", "-m", "peg f2")
        p2 = g(integ_seed, "rev-parse", "HEAD")
        g(integ_seed, "push", "-q")

        p = subprocess.run(
            [sys.executable, str(SCRIPT), "--repo", str(integ_clone),
             "--source-branch", "origin/main", "--submodule", "Src/FTL",
             "--ftl-repo", str(ftl_clone), "--fetch", f2],
            capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        out = json.loads(p.stdout)
        self.assertEqual(out["queries"][0]["status"], "found")
        self.assertEqual(out["queries"][0]["pegging"], p2[:7])
        self.assertEqual(out["branch_tip"]["sha"], p2)
        self.assertEqual(out["fetch"]["repositories"],
                         {"FTL": "succeeded", "integration": "succeeded"})

    def test_fetch_failure_stops_instead_of_using_stale_snapshot(self):
        out = self.run_tool(self.f[5], "--fetch", expect_code=3)
        self.assertEqual(out["error_code"], "FETCH_FAILED")
        self.assertEqual(out["fetch"]["repositories"]["integration"], "failed")

    def test_not_found_in_ftl(self):
        out = self.run_tool("deadbeefdeadbee")
        self.assertEqual(out["queries"][0]["status"], "not_found_in_ftl")

    def test_thorough_agrees_with_binary(self):
        out = self.run_tool(self.f[1], "--thorough")
        q = out["queries"][0]
        self.assertEqual(q["search"], "linear")
        self.assertEqual(q["pegging"], self.p2[:7])

    # ------------------------------------------------------------ 입출력

    def test_help_shows_complete_src_path_example(self):
        p = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            capture_output=True, text=True)
        self.assertEqual(p.returncode, 0)
        self.assertIn("--submodule", p.stdout)
        self.assertIn("Src/FTL", p.stdout)
        for spec in ("Src/HAL=~/HAL", "Src/Shared=~/Shared", "Src/FIL=~/FIL"):
            self.assertIn(spec, p.stdout)
        self.assertIn("integration gitlink 경로", p.stdout)
        self.assertIn("사용자에게", p.stdout)
        self.assertIn("develop_XXX", p.stdout)
        self.assertIn("추측 금지", p.stdout)
        self.assertNotIn("develop_Evan", p.stdout)

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
        out = self.run_tool(self.f[0], "--source-branch", "no-such-branch",
                            expect_code=2)
        self.assertEqual(out["error_code"], "BRANCH_NOT_FOUND")

    # ------------------------------------------------------------ --output

    def test_output_writes_full_json_stdout_gets_summary(self):
        """--output: 전체 JSON은 파일, stdout은 sha별 digest 요약."""
        path = Path(self._tmp.name) / "resolve_out.json"
        out = self.run_tool(self.f[2], "--output", str(path))
        self.assertTrue(out["output_written"])
        self.assertNotIn("peggings", out)  # 상세는 파일에만
        self.assertEqual(out["summary"]["queries_total"], 1)
        self.assertEqual(out["summary"]["by_status"],
                         {"found": 1, "not_pegged": 0, "not_found_in_ftl": 0})
        self.assertEqual(out["summary"]["peggings_total"], 1)
        self.assertEqual(out["queries"][0]["status"], "found")
        self.assertNotIn("search", out["queries"][0])  # digest는 축약형
        self.assertNotIn(str(path), json.dumps(out))  # 로컬 경로 금지 정책

        full = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(full["schema_version"], 1)
        self.assertEqual(full["mode"], "resolve")
        self.assertTrue(full["peggings"][0]["ftl"]["batch"])
        self.assertEqual(full["queries"][0]["search"], "pickaxe")

    def test_output_unwritable_path(self):
        out = self.run_tool(
            self.f[1], "--output",
            str(Path(self._tmp.name) / "no-such-dir" / "out.json"),
            expect_code=3)
        self.assertEqual(out["error_code"], "OUTPUT_WRITE_FAILED")

    # ------------------------------------------------------------ --progress

    def run_raw(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT),
             "--repo", str(self.integ), "--source-branch", "main",
             "--submodule", "Src/FTL", "--ftl-repo", str(self.ftl), *args],
            capture_output=True, text=True)

    def test_progress_logs_stages_to_stderr(self):
        """--progress: stderr에 단계·건수 진행 로그, stdout JSON 계약은 불변."""
        p = self.run_raw("--progress", self.f[2])
        self.assertEqual(p.returncode, 0)
        out = json.loads(p.stdout)
        self.assertEqual(out["queries"][0]["status"], "found")
        self.assertIn("[resolve_sha", p.stderr)
        self.assertIn("pegging 열거", p.stderr)
        self.assertIn("pegging 역추적", p.stderr)
        self.assertIn("동반 세트", p.stderr)
        # 경로 금지 정책은 진행 로그에도 적용된다
        self.assertNotIn(self._tmp.name, p.stderr)

    def test_progress_off_by_default_but_timings_always_reported(self):
        """기본(비TTY)은 stderr 무출력 — 단계별 소요 시간은 항상 timings로."""
        p = self.run_raw(self.f[2])
        self.assertEqual(p.returncode, 0)
        self.assertEqual(p.stderr, "")
        out = json.loads(p.stdout)
        for key in ("peggings", "locate", "companions", "total"):
            self.assertGreaterEqual(out["timings"][key], 0)

    def test_output_summary_keeps_timings(self):
        path = Path(self._tmp.name) / "resolve_timings.json"
        out = self.run_tool(self.f[2], "--output", str(path))
        self.assertIn("total", out["timings"])


if __name__ == "__main__":
    unittest.main()
