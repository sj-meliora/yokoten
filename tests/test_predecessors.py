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
VIZ = Path(__file__).resolve().parent.parent / "predecessors_viz.py"

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

        # 같은 파일이지만 서로 먼 부근을 건드리는 케이스 (risk=same_file 검증)
        lines = [f"line{i}" for i in range(1, 201)]
        cls.c6 = commit_file(cls.ftl, "big.txt", "\n".join(lines) + "\n",
                             "AGCD-6: add big table")
        lines[9] = "line10-modified"
        cls.c7 = commit_file(cls.ftl, "big.txt", "\n".join(lines) + "\n",
                             "AGCD-7: tweak head")
        lines[149] = "line150-modified"
        cls.c8 = commit_file(cls.ftl, "big.txt", "\n".join(lines) + "\n",
                             "AGCD-8: tweak tail")

        # 줄 번호 드리프트 케이스 — c10이 8행을 고친 뒤 c11이 위에 50줄을
        # 삽입해 같은 줄이 58행으로 밀림. blame 기반이라 c12는 c10을
        # required_first로 정확히 지목해야 한다 (좌표 비교로는 불가능).
        drift = [f"d{i}" for i in range(1, 11)]
        cls.c9 = commit_file(cls.ftl, "drift.txt", "\n".join(drift) + "\n",
                             "AGCD-9: add drift file")
        drift[7] = "d8-modified"
        cls.c10 = commit_file(cls.ftl, "drift.txt", "\n".join(drift) + "\n",
                              "AGCD-10: tweak d8")
        drift = [f"pad{i}" for i in range(1, 51)] + drift
        cls.c11 = commit_file(cls.ftl, "drift.txt", "\n".join(drift) + "\n",
                              "AGCD-11: prepend padding")
        drift[57] = "d8-modified-again"
        cls.c12 = commit_file(cls.ftl, "drift.txt", "\n".join(drift) + "\n",
                              "AGCD-12: tweak d8 again")

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
             "--repo", str(cls.integ), "--source-branch", "main",
             "--submodule", "Src/FTL", "--ftl-repo", str(cls.ftl),
             "--target-branch", "develop", *args],
            capture_output=True, text=True)
        assert p.returncode == expect_code, \
            f"exit {p.returncode} != {expect_code}: {p.stdout} {p.stderr}"
        return json.loads(p.stdout)

    # ------------------------------------------------------------ 판정

    def test_predecessors_with_evidence_risk_and_pegging(self):
        """diff 부근 의존만 선행 판정 — 부근 무관 미반영은 건수로만 보고."""
        out = self.run_tool(self.c4)
        self.assertEqual(out["mode"], "predecessors")
        self.assertEqual(out["target"]["ref"], "develop")
        q = out["queries"][0]
        self.assertEqual(q["status"], "found")
        self.assertEqual(q["pegging"], self.p3[:7])
        # 질의 커밋 자신의 메타 — 리포트 접힌 헤더에서도 제목이 보여야 한다
        self.assertEqual(q["subject"], "AGCD-4: finish a")
        self.assertIsNotNone(q["date"])
        self.assertEqual(q["self"],
                         {"applied": "not_applied", "ims_keys": ["AGCD-4"]})
        # c1은 patch 등가로 반영 완료 → 목록 제외, applied_total로 집계
        self.assertEqual(q["applied_total"], 1)
        self.assertEqual(q["merges_skipped"], 0)
        self.assertFalse(q["predecessors_truncated"])
        # c2는 다른 파일(b.txt) — 시간상 앞서도 선행이 아니다 (노이즈 제외)
        self.assertEqual(q["predecessors_total"], 1)
        self.assertEqual(q["unrelated_unapplied_total"], 1)

        (c3,) = q["predecessors"]
        self.assertEqual(c3["sha"], self.c3)
        self.assertEqual(c3["pegging"], self.p3[:7])
        self.assertTrue(c3["same_batch"])
        self.assertEqual(c3["ims_keys"], ["AGCD-3"])
        self.assertEqual(c3["applied_evidence"], "none")
        self.assertEqual(c3["risk"], "required_first")
        self.assertEqual(c3["required_by"], [self.c4])  # F가 직접 지목
        self.assertEqual(c3["overlap_paths"], ["a.txt"])
        self.assertEqual(c3["same_file_paths"], [])
        self.assertEqual(c3["companions_moved"], [])
        self.assertEqual(c3["companion_links"], [])
        # 질의 커밋 자신의 pegging은 FTL 단독 — sibling 없음
        self.assertEqual(q["companion_links"], [])

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
        # 질의 커밋 자신의 pegging에 같이 배달된 sibling gitlink sha
        self.assertEqual(q["companion_links"],
                         [{"path": "Src/HAL", "from": H1, "to": H2}])

    def test_not_pegged_query_still_scans_ancestry(self):
        """미배달 sha도 ancestry 기준 사전 점검이 가능하다."""
        out = self.run_tool(self.c5)
        q = out["queries"][0]
        self.assertEqual(q["status"], "not_pegged")
        self.assertIsNone(q["pegging"])
        self.assertEqual(q["self"]["applied"], "not_applied")
        # c5는 b.txt 변경 — b.txt를 만든 c2만 선행, a.txt 쪽 c3·c4는 제외
        (c2,) = q["predecessors"]
        self.assertEqual(c2["sha"], self.c2)
        self.assertEqual(c2["risk"], "required_first")
        self.assertEqual(c2["required_by"], [self.c5])
        self.assertEqual(c2["overlap_paths"], ["b.txt"])
        self.assertEqual(c2["applied_evidence"], "ims_key")  # 변형 pick 감지
        self.assertEqual(q["unrelated_unapplied_total"], 2)
        # F가 미배달이라 same_batch는 판정 불가(null), pegging은 각자 보고
        self.assertEqual(c2["pegging"], self.p2[:7])
        self.assertIsNone(c2["same_batch"])
        self.assertEqual(c2["companions_moved"], ["Src/HAL"])
        # sibling gitlink의 전후 sha도 함께 보고한다
        self.assertEqual(c2["companion_links"],
                         [{"path": "Src/HAL", "from": H1, "to": H2}])

    def test_same_file_far_region_is_excluded(self):
        """부근(hunk ±3줄) 겹침만 선행 — 같은 파일 먼 부근·다른 파일은 제외."""
        out = self.run_tool(self.c8)  # big.txt 150행 수정
        q = out["queries"][0]
        preds = {p["sha"]: p for p in q["predecessors"]}
        # 파일을 만든 커밋(전체 범위) — 부근 겹침으로 required_first
        self.assertEqual(set(preds), {self.c6})
        self.assertEqual(preds[self.c6]["risk"], "required_first")
        self.assertEqual(preds[self.c6]["overlap_paths"], ["big.txt"])
        # c7(같은 파일 10행 vs 150행)·c2(다른 파일)는 시간상 앞서도 선행이
        # 아니다 — 목록에서 빠지고 건수로만 보고
        self.assertNotIn(self.c7, preds)
        self.assertNotIn(self.c2, preds)
        self.assertEqual(q["predecessors_total"], 1)
        self.assertEqual(q["unrelated_unapplied_total"], 5)  # c2·c3·c4·c5·c7

    def test_blame_survives_line_drift(self):
        """사이 커밋이 줄을 밀어내도 blame은 진짜 의존을 지목한다."""
        out = self.run_tool(self.c12)  # drift.txt 58행(=원래 8행) 수정
        q = out["queries"][0]
        preds = {p["sha"]: p for p in q["predecessors"]}
        self.assertEqual(set(preds), {self.c9, self.c10})
        # c10은 8행을 고쳤고 지금은 58행 — 좌표는 50줄 어긋나지만 직접 의존
        self.assertEqual(preds[self.c10]["risk"], "required_first")
        self.assertEqual(preds[self.c10]["required_by"], [self.c12])
        self.assertEqual(preds[self.c10]["overlap_paths"], ["drift.txt"])
        # 부근의 나머지 줄들을 만든 c9도 blame에 걸린다 (context 의존) —
        # F가 직접 지목하고 c10의 부근 blame으로도 연쇄 지목된다
        self.assertEqual(preds[self.c9]["risk"], "required_first")
        self.assertEqual(set(preds[self.c9]["required_by"]),
                         {self.c10, self.c12})
        # c11(위쪽 50줄 삽입)은 같은 파일이지만 F의 변경 부근이 아님 — 제외
        self.assertNotIn(self.c11, preds)
        self.assertEqual(q["unrelated_unapplied_total"], 8)

    def test_limit_keeps_most_recent(self):
        """--limit 절단은 오래된 쪽을 잘라내고 최근 커밋을 남긴다."""
        out = self.run_tool(self.c12, "--limit", "1")
        q = out["queries"][0]
        self.assertTrue(q["predecessors_truncated"])
        self.assertEqual(q["predecessors_total"], 2)  # c9·c10 중 최근 c10만
        self.assertEqual([p["sha"] for p in q["predecessors"]], [self.c10])

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

        graph = data["graph"]
        self.assertFalse(graph["truncated"])
        nodes = {n["sha"]: n for n in graph["nodes"]}
        self.assertEqual(set(nodes), {self.c1, self.c2, self.c3, self.c4})
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

    def test_html_report_shares_one_graph_across_queries(self):
        """질의가 여러 개여도 그래프는 합집합 한 벌 — 파일 크기 스케일 대책."""
        report = Path(self._tmp.name) / "multi.html"
        out = self.run_tool(self.c4, self.c5, "--html", str(report))
        self.assertEqual([q["status"] for q in out["queries"]],
                         ["found", "not_pegged"])
        html = report.read_text(encoding="utf-8")
        m = re.search(
            r'<script id="data" type="application/json">(.*?)</script>',
            html, re.S)
        data = json.loads(m.group(1))
        self.assertNotIn("graphs", data)  # 질의별 그래프 아님
        nodes = {n["sha"]: n for n in data["graph"]["nodes"]}
        # 합집합: c5의 조상까지 포함해 c1..c5 전부, 각 한 번씩
        self.assertEqual(set(nodes), {self.c1, self.c2, self.c3,
                                      self.c4, self.c5})
        self.assertEqual(len(data["graph"]["nodes"]), 5)
        self.assertEqual(nodes[self.c5]["status"], "not_applied")
        self.assertEqual(nodes[self.c2]["status"], "key_matched")
        # 질의는 공유 그래프의 시작 커밋만 가리킨다
        self.assertEqual([q["ftl_sha"] for q in data["queries"]],
                         [self.c4, self.c5])

    def test_emit_graph_is_opt_in(self):
        """--emit-graph는 orchestration용 additive 옵션 — 기본은 미포함."""
        out = self.run_tool(self.c4)
        self.assertNotIn("graph", out)
        out = self.run_tool(self.c4, "--emit-graph")
        self.assertTrue(out["graph"]["nodes"])

    def test_report_regenerated_from_saved_output_matches_direct(self):
        """--emit-graph --output 저장본 → viz CLI 재생성이 직접 --html과 동등.

        수십 분짜리 스캔을 재실행하지 않고 저장된 JSON에서 보고서만 다시
        만드는 흐름 — 질의·그래프가 저장본 그대로 내장되어야 한다.
        """
        saved = Path(self._tmp.name) / "regen_src.json"
        report = Path(self._tmp.name) / "regen.html"
        self.run_tool(self.c4, self.c5, "--emit-graph", "--output", str(saved))
        full = json.loads(saved.read_text(encoding="utf-8"))
        self.assertIn("graph", full)  # --emit-graph면 그래프도 저장된다

        p = subprocess.run(
            [sys.executable, str(VIZ), str(saved), "--html", str(report)],
            capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        out = json.loads(p.stdout)
        self.assertTrue(out["ok"])
        self.assertTrue(out["graph_embedded"])
        self.assertEqual(out["queries_total"], 2)

        html = report.read_text(encoding="utf-8")
        m = re.search(
            r'<script id="data" type="application/json">(.*?)</script>',
            html, re.S)
        data = json.loads(m.group(1))
        self.assertEqual(data["queries"], full["queries"])
        self.assertEqual(data["graph"], full["graph"])
        self.assertTrue(data["regenerated"])
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
        out = self.run_tool(self.c4, "--target-branch", "no-such-branch",
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
        self.assertIn("--target-branch", p.stdout)
        self.assertIn("origin/develop", p.stdout)
        self.assertIn("추측 금지", p.stdout)
        self.assertIn("사용자에게", p.stdout)

    # ------------------------------------------------------------ 공유 스캔

    def test_batch_scan_matches_individual_runs(self):
        """head 단위 공유 스캔 — 일괄 실행이 개별 실행과 판정이 동일해야 한다.

        c2·c4는 head(c12)의 스캔 결과 그래프에서 bloodline으로 복원되므로,
        개별 T...F 스캔과 predecessors 목록·순서·집계가 전부 같아야 한다.
        """
        batch = self.run_tool(self.c2, self.c4, self.c12)
        for sha in (self.c2, self.c4, self.c12):
            single = self.run_tool(sha)
            b = next(q for q in batch["queries"] if q["input"] == sha)
            self.assertEqual(b, single["queries"][0], f"sha {sha[:7]} 불일치")

    # ------------------------------------------------------------ --output

    def test_output_writes_full_json_stdout_gets_summary(self):
        """--output: 선행 상세는 파일, stdout은 sha별 판정 digest."""
        path = Path(self._tmp.name) / "pred_out.json"
        out = self.run_tool(self.c4, self.c5, "--output", str(path))
        self.assertTrue(out["output_written"])
        self.assertEqual(out["summary"]["queries_total"], 2)
        self.assertEqual(out["summary"]["by_status"],
                         {"found": 1, "not_pegged": 1, "not_found_in_ftl": 0})
        self.assertEqual(out["summary"]["with_missing_predecessors"], 2)
        q, q5 = out["queries"]
        self.assertEqual(q["applied"], "not_applied")
        self.assertEqual(q["subject"], "AGCD-4: finish a")
        # 선행은 확정(미반영)/미확정(key 흔적)으로 나뉘어 실린다 — 각 오래된 순
        self.assertEqual([p["short"] for p in q["predecessors_confirmed"]],
                         [self.c3[:7]])
        self.assertEqual(q["predecessors_unconfirmed"], [])  # c2는 부근 무관
        self.assertEqual(q["unrelated_unapplied_total"], 1)
        self.assertEqual(q["predecessors_confirmed"][0]["required_by"],
                         [self.c4])
        # c5 질의 — key 흔적 있는 선행(c2)은 미확정 쪽에 실린다
        unc = q5["predecessors_unconfirmed"]
        self.assertEqual([p["short"] for p in unc], [self.c2[:7]])
        # sibling gitlink가 같이 움직였으면 그 sha도 digest에 보인다
        self.assertEqual(unc[0]["siblings"], [{"path": "Src/HAL", "sha": H2}])
        self.assertNotIn("predecessors", q)  # blame 상세는 파일에서만
        self.assertGreater(q["predecessors_total"], 0)
        self.assertNotIn(str(path), json.dumps(out))  # 로컬 경로 금지 정책

        full = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(full["schema_version"], 1)
        self.assertEqual(full["mode"], "predecessors")
        fq = full["queries"][0]
        self.assertEqual(fq["predecessors_total"], q["predecessors_total"])
        self.assertTrue(all("risk" in p and "required_by" in p
                            for p in fq["predecessors"]))

    def test_output_digest_respects_limit_and_fixed_schema(self):
        """digest: 확정+미확정 합이 --limit 상한, 스키마는 상태와 무관하게 고정."""
        path = Path(self._tmp.name) / "pred_digest.json"
        out = self.run_tool(self.c12, "deadbeefdeadbee", "--limit", "1",
                            "--output", str(path))
        self.assertEqual(out["summary"]["by_status"],
                         {"found": 0, "not_pegged": 1, "not_found_in_ftl": 1})
        q, missing = out["queries"]
        # 절단 시에도 두 목록의 합이 limit 개수 — 최근 커밋 우선
        self.assertTrue(q["predecessors_truncated"])
        self.assertEqual(len(q["predecessors_confirmed"])
                         + len(q["predecessors_unconfirmed"]), 1)
        self.assertEqual(q["predecessors_confirmed"][0]["short"], self.c10[:7])
        self.assertEqual(q["predecessors_total"], 2)
        # 해석 불가 질의도 같은 키 집합 — 실행마다 스키마가 달라지지 않는다
        self.assertEqual(set(missing), set(q))
        self.assertIsNone(missing["predecessors_confirmed"])
        self.assertIsNone(missing["predecessors_unconfirmed"])
        self.assertIsNone(missing["unrelated_unapplied_total"])

    def test_output_unwritable_path(self):
        out = self.run_tool(
            self.c4, "--output",
            str(Path(self._tmp.name) / "no-such-dir" / "out.json"),
            expect_code=3)
        self.assertEqual(out["error_code"], "OUTPUT_WRITE_FAILED")


class ChainTest(unittest.TestCase):
    """연쇄 의존 — F의 blame에 직접 안 보이는 오래된 diff 부근 의존도 포함.

    blame은 줄의 마지막 수정 커밋만 지목한다. 같은 줄을 k1 → k2 → k3(F)가
    연달아 고쳤으면 F^의 blame은 k2만 보이지만, k2를 pick하려면 k1이
    필요하므로 k1도 k2 경유(required_by)로 선행 목록에 올라야 한다.

    픽스처: FTL(main) b0 → k0(ch.txt 100줄, target에 깨끗이 pick됨)
    → k1(50행) → kf(90행 — 먼 부근) → k2(50행) → k3(50행, F).
    """

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        base = Path(cls._tmp.name)
        cls.ftl, cls.integ = base / "ftl", base / "integ"
        cls.ftl.mkdir()
        cls.integ.mkdir()
        g(cls.ftl, "init", "-q", "-b", "main")
        cls.b0 = commit_file(cls.ftl, "base.txt", "base\n", "base")
        lines = [f"ch{i}" for i in range(1, 101)]
        cls.k0 = commit_file(cls.ftl, "ch.txt", "\n".join(lines) + "\n",
                             "AGCD-50: add ch file")
        lines[49] = "ch50-k1"
        cls.k1 = commit_file(cls.ftl, "ch.txt", "\n".join(lines) + "\n",
                             "AGCD-51: tweak ch50")
        lines[89] = "ch90-kf"
        cls.kf = commit_file(cls.ftl, "ch.txt", "\n".join(lines) + "\n",
                             "AGCD-59: tweak ch90 (far)")
        lines[49] = "ch50-k2"
        cls.k2 = commit_file(cls.ftl, "ch.txt", "\n".join(lines) + "\n",
                             "AGCD-52: tweak ch50 again")
        lines[49] = "ch50-k3"
        cls.k3 = commit_file(cls.ftl, "ch.txt", "\n".join(lines) + "\n",
                             "AGCD-53: tweak ch50 final")
        g(cls.ftl, "checkout", "-q", "-b", "develop", cls.b0)
        g(cls.ftl, "cherry-pick", cls.k0,
          env={"GIT_COMMITTER_DATE": "2030-01-02T03:04:05 +0000"})
        g(cls.ftl, "checkout", "-q", "main")
        g(cls.integ, "init", "-q", "-b", "main")
        g(cls.integ, "update-index", "--add",
          "--cacheinfo", f"160000,{cls.k3},Src/FTL")
        g(cls.integ, "commit", "-q", "-m", "peg tip")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_transitive_dependency_is_included_via_chain(self):
        p = subprocess.run(
            [sys.executable, str(SCRIPT),
             "--repo", str(self.integ), "--source-branch", "main",
             "--submodule", "Src/FTL", "--ftl-repo", str(self.ftl),
             "--target-branch", "develop", self.k3],
            capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        q = json.loads(p.stdout)["queries"][0]
        # 오래된 순 = pick 적용 순서: k1 → k2
        self.assertEqual([x["sha"] for x in q["predecessors"]],
                         [self.k1, self.k2])
        k1, k2 = q["predecessors"]
        # k2는 F(k3)의 blame이 직접 지목
        self.assertEqual(k2["risk"], "required_first")
        self.assertEqual(k2["required_by"], [self.k3])
        # k1은 F의 blame에는 안 보이지만 k2의 부근 blame으로 연쇄 지목
        self.assertEqual(k1["risk"], "required_first")
        self.assertEqual(k1["required_by"], [self.k2])
        self.assertEqual(k1["overlap_paths"], ["ch.txt"])
        # 먼 부근(90행)만 고친 kf는 선행이 아니다 — 건수로만 보고
        self.assertEqual(q["unrelated_unapplied_total"], 1)
        # 기반영 커밋(k0)에서 연쇄가 멈춘다 — target에 내용이 있으므로
        self.assertEqual(q["applied_total"], 1)


class ClosureCostTest(unittest.TestCase):
    """연쇄 확장 비용 — 커밋별 diff·blame은 질의 간 공유 캐시로 1회만.

    같은 줄을 연달아 수정한 N개 커밋 전체를 한 번에 질의하면(구간 일괄
    분석과 같은 형태) 각 질의의 closure가 크게 겹친다. 캐시가 없으면
    blame·diff-tree가 O(N²/2)회 돌아 실사용 규모(수백 건)에서 실행이
    수십 시간으로 터진다 — GIT_TRACE로 blame 호출 수가 고유 커밋 수
    수준(≤2N)에 머무는지 고정한다.
    """

    N = 10

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        base = Path(cls._tmp.name)
        cls.ftl, cls.integ = base / "ftl", base / "integ"
        cls.ftl.mkdir()
        cls.integ.mkdir()
        g(cls.ftl, "init", "-q", "-b", "main")
        lines = [f"l{i}" for i in range(1, 21)]
        b0 = commit_file(cls.ftl, "hot.c", "\n".join(lines) + "\n", "base")
        cls.shas = []
        for k in range(cls.N):
            lines[4] = f"l5-rev{k}"
            cls.shas.append(commit_file(cls.ftl, "hot.c",
                                        "\n".join(lines) + "\n",
                                        f"AGCD-{100 + k}: rev{k}"))
        g(cls.ftl, "branch", "develop", b0)
        g(cls.integ, "init", "-q", "-b", "main")
        g(cls.integ, "update-index", "--add",
          "--cacheinfo", f"160000,{cls.shas[-1]},Src/FTL")
        g(cls.integ, "commit", "-q", "-m", "peg tip")

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_blame_calls_scale_with_unique_commits_not_queries(self):
        trace = Path(self._tmp.name) / "trace.log"
        p = subprocess.run(
            [sys.executable, str(SCRIPT),
             "--repo", str(self.integ), "--source-branch", "main",
             "--submodule", "Src/FTL", "--ftl-repo", str(self.ftl),
             "--target-branch", "develop", *self.shas],
            capture_output=True, text=True,
            env={**os.environ, "GIT_TRACE": str(trace)})
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        out = json.loads(p.stdout)
        # 판정 자체는 그대로 — tip의 선행은 나머지 전부(같은 줄 연쇄)
        tip = next(q for q in out["queries"]
                   if q["ftl_sha"] == self.shas[-1])
        self.assertEqual(tip["predecessors_total"], self.N - 1)
        self.assertEqual([x["sha"] for x in tip["predecessors"]],
                         self.shas[:-1])  # 오래된 순
        blames = re.findall(r"trace: built-in: git blame",
                            trace.read_text())
        # 캐시 없이는 질의별 closure 재확장으로 ~N²/2회(N=10이면 45+)
        self.assertLessEqual(len(blames), 2 * self.N)


def commit_dated(repo: Path, name: str, content: str, msg: str,
                 date: str) -> str:
    (repo / name).write_text(content)
    g(repo, "add", ".")
    g(repo, "commit", "-q", "-m", msg,
      env={"GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date})
    return g(repo, "rev-parse", "HEAD")


class WindowTest(unittest.TestCase):
    """--since 판정 창 — 창 밖 조상 미판정과 clipped/excluded 신호.

    픽스처: FTL(main) base(2020) → o1(2020) → r1(현재) → r2(현재),
    target(develop)은 base에서 분기해 pick 없음. 2023 창을 걸면 o1이
    창 밖으로 잘려 미판정이 되어야 한다.
    """

    OLD = "2020-01-01T00:00:00 +0000"

    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        base = Path(cls._tmp.name)
        cls.ftl, cls.integ = base / "ftl", base / "integ"
        cls.ftl.mkdir()
        cls.integ.mkdir()
        g(cls.ftl, "init", "-q", "-b", "main")
        cls.b1 = commit_dated(cls.ftl, "base.txt", "base\n", "base", cls.OLD)
        cls.o1 = commit_dated(cls.ftl, "o.txt", "o1\n",
                              "AGCD-90: old change", cls.OLD)
        cls.r1 = commit_file(cls.ftl, "r.txt", "r1\n", "AGCD-91: recent dep")
        cls.r2 = commit_file(cls.ftl, "r.txt", "r1\nr2\n",
                             "AGCD-92: recent tip")
        g(cls.ftl, "branch", "develop", cls.b1)
        g(cls.integ, "init", "-q", "-b", "main")
        g(cls.integ, "update-index", "--add",
          "--cacheinfo", f"160000,{cls.r2},Src/FTL")
        g(cls.integ, "commit", "-q", "-m", "peg tip")

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

    def test_unbounded_scan_judges_full_bloodline(self):
        out = self.run_tool(self.r2)
        q = out["queries"][0]
        self.assertIsNone(out["window"])
        self.assertIsNone(q["window_clipped"])
        # r2는 r.txt 변경 — r.txt를 만든 r1만 선행, o1(o.txt)은 부근 무관
        self.assertEqual(q["predecessors_total"], 1)
        self.assertEqual([p["sha"] for p in q["predecessors"]], [self.r1])
        self.assertEqual(q["unrelated_unapplied_total"], 1)

    def test_window_limits_scan_and_flags_clipping(self):
        out = self.run_tool(self.r2, "--since", "2023-01-01")
        q = out["queries"][0]
        self.assertEqual(out["window"]["since"], "2023-01-01")
        self.assertEqual(out["window"]["excluded_total"], 1)  # o1 미판정
        self.assertTrue(q["window_clipped"])
        self.assertEqual(q["predecessors_total"], 1)
        self.assertEqual(q["predecessors"][0]["sha"], self.r1)
        self.assertEqual(q["unrelated_unapplied_total"], 0)  # 창 밖 o1은 미판정
        self.assertEqual(q["self"]["applied"], "not_applied")  # 창 내 판정 정상

    def test_query_outside_window_is_unjudged(self):
        out = self.run_tool(self.o1, "--since", "2023-01-01")
        q = out["queries"][0]
        self.assertEqual(q["self"]["applied"], "unknown")
        self.assertTrue(q["window_clipped"])
        self.assertIsNone(q["predecessors"])
        self.assertTrue(any("창 밖" in n for n in q["notes"]))

    def test_window_is_embedded_in_html_report(self):
        """--since 실행의 --html — 판정 창 정보가 리포트 데이터에도 실린다."""
        report = Path(self._tmp.name) / "window.html"
        self.run_tool(self.r2, "--since", "2023-01-01", "--html", str(report))
        m = re.search(
            r'<script id="data" type="application/json">(.*?)</script>',
            report.read_text(encoding="utf-8"), re.S)
        data = json.loads(m.group(1))
        self.assertEqual(data["window"],
                         {"since": "2023-01-01", "excluded_total": 1})
        self.assertTrue(data["queries"][0]["window_clipped"])

    def test_window_containing_everything_matches_unbounded(self):
        plain = self.run_tool(self.r2)["queries"][0]
        wide = self.run_tool(self.r2, "--since", "2000-01-01")["queries"][0]
        for key in ("predecessors", "predecessors_total",
                    "unrelated_unapplied_total", "applied_total",
                    "merges_skipped", "self"):
            self.assertEqual(plain[key], wide[key], key)
        self.assertFalse(wide["window_clipped"])  # 경계가 target 이력에 닿음


if __name__ == "__main__":
    unittest.main()
