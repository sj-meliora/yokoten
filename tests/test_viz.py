"""predecessors_viz.py 회귀 테스트 — git 없이 순수 렌더링 계층만 검증.

CLI를 통한 end-to-end 리포트 생성은 test_predecessors.py가 검증한다.
여기서는 모듈 경계(분석과 시각화의 분리)가 지켜지는지와 payload → HTML
변환 계약만 본다.
"""

import json
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import predecessors_viz

VIZ = Path(predecessors_viz.__file__).resolve()


class VizTest(unittest.TestCase):
    payload = {
        "branch": "origin/develop_XXX",
        "branch_tip": {"sha": "a" * 40, "short": "a" * 7},
        "submodule": "Src/FTL",
        "target": {"ref": "origin/develop", "sha": "b" * 40,
                   "short": "b" * 7},
        "generated": "2026-08-01 00:00 UTC",
        "max_graph_nodes": 2000,
        "queries": [],
        "graph": {"nodes": [], "truncated": False},
    }

    def test_write_report_embeds_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.html"
            self.assertIsNone(
                predecessors_viz.write_report(str(path), self.payload))
            html = path.read_text(encoding="utf-8")
        self.assertNotIn("__DATA__", html)
        m = re.search(
            r'<script id="data" type="application/json">(.*?)</script>',
            html, re.S)
        self.assertEqual(json.loads(m.group(1)), self.payload)

    def test_write_report_escapes_script_close(self):
        """subject에 </script>가 있어도 데이터 블록이 조기 종료되지 않는다."""
        payload = dict(self.payload)
        payload["graph"] = {"truncated": False, "nodes": [{
            "sha": "c" * 40, "short": "c" * 7, "parents": [],
            "date": "2026-01-01", "subject": "</script><b>x",
            "ims_keys": [], "status": "not_applied"}]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "r.html"
            self.assertIsNone(
                predecessors_viz.write_report(str(path), payload))
            html = path.read_text(encoding="utf-8")
        blocks = re.findall(
            r'<script id="data" type="application/json">(.*?)</script>',
            html, re.S)
        self.assertEqual(len(blocks), 1)
        # "<\/"는 JSON 표준 escape — 파싱하면 원문 그대로 복원된다
        node = json.loads(blocks[0])["graph"]["nodes"][0]
        self.assertEqual(node["subject"], "</script><b>x")

    def test_write_report_failure_returns_reason(self):
        why = predecessors_viz.write_report("/no-such-dir/r.html",
                                            self.payload)
        self.assertIsNotNone(why)

    def test_pure_rendering_layer(self):
        """모듈 경계 — 시각화 계층은 git·분석 코드에 의존하지 않는다."""
        src = Path(predecessors_viz.__file__).read_text(encoding="utf-8")
        self.assertNotIn("subprocess", src)
        self.assertNotIn("import resolve_sha", src)
        self.assertNotIn("from resolve_sha", src)


def saved_predecessors(**extra) -> dict:
    """predecessors.py --output 전체 JSON의 최소 형태 (질의 1건)."""
    return {
        "schema_version": 1, "ok": True, "mode": "predecessors",
        "branch": "origin/develop_XXX",
        "branch_tip": {"sha": "a" * 40, "short": "a" * 7},
        "submodule": "Src/FTL",
        "target": {"ref": "origin/develop", "sha": "b" * 40, "short": "b" * 7},
        "window": None,
        "queries": [{
            "input": "c" * 7, "ftl_sha": "c" * 40, "ftl_short": "c" * 7,
            "subject": "AGCD-1: x", "date": "2026-01-01",
            "status": "found", "pegging": "d" * 7, "companion_links": [],
            "self": {"applied": "not_applied", "ims_keys": ["AGCD-1"]},
            "predecessors": [], "predecessors_total": 0,
            "predecessors_truncated": False, "applied_total": 0,
            "merges_skipped": 0, "window_clipped": None, "notes": [],
        }],
        "fetch": {}, "notes": [],
        **extra,
    }


class RerenderTest(unittest.TestCase):
    """저장된 --output JSON → 보고서 재생성 (payload 조립 + CLI)."""

    def test_payload_from_predecessors_output_without_graph(self):
        payload, why = predecessors_viz.payload_from_saved(saved_predecessors())
        self.assertIsNone(why)
        self.assertEqual(payload["branch"], "origin/develop_XXX")
        self.assertEqual(payload["target"]["ref"], "origin/develop")
        self.assertEqual(len(payload["queries"]), 1)
        # 그래프 없이 저장된 실행 — None으로 내장돼 브라우저가 경고를 띄운다
        self.assertIsNone(payload["graph"])
        self.assertTrue(payload["regenerated"])
        self.assertIn("generated", payload)

    def test_payload_from_predecessors_output_with_graph_and_window(self):
        graph = {"nodes": [{"sha": "c" * 40, "short": "c" * 7, "parents": [],
                            "date": "2026-01-01", "subject": "AGCD-1: x",
                            "ims_keys": ["AGCD-1"], "status": "not_applied"}],
                 "truncated": False}
        saved = saved_predecessors(
            graph=graph, max_graph_nodes=2000,
            window={"since": "1.year", "excluded_total": 3})
        payload, why = predecessors_viz.payload_from_saved(saved)
        self.assertIsNone(why)
        self.assertEqual(payload["graph"], graph)
        self.assertEqual(payload["max_graph_nodes"], 2000)
        self.assertEqual(payload["window"],
                         {"since": "1.year", "excluded_total": 3})

    def test_payload_from_analyze_output(self):
        pred = saved_predecessors()
        saved = {
            "schema_version": 1, "ok": True, "mode": "analyze",
            "branch": "origin/develop_XXX", "submodule": "Src/FTL",
            "target": pred["target"],
            "range": {"from": "e" * 40, "from_short": "e" * 7,
                      "to": "c" * 40, "to_short": "c" * 7,
                      "commits_total": 2},
            "resolve": {"peggings": [{"pegging": {"sha": "d" * 40}}],
                        "notes": ["n1"]},
            "predecessors": pred, "fetch": {}, "notes": [],
        }
        payload, why = predecessors_viz.payload_from_saved(saved)
        self.assertIsNone(why)
        self.assertEqual(payload["queries"], pred["queries"])
        self.assertEqual(payload["range"]["commits_total"], 2)
        self.assertEqual(payload["resolve"]["notes"], ["n1"])
        self.assertEqual(len(payload["resolve"]["peggings"]), 1)

    def test_payload_rejects_digest_failure_and_unknown(self):
        for bad in (
            {**saved_predecessors(), "output_written": True},  # stdout 요약
            {"ok": False, "error_code": "FETCH_FAILED"},        # 실패 JSON
            {"ok": True, "mode": "resolve"},                    # 다른 스크립트
            ["not-a-dict"],
        ):
            payload, why = predecessors_viz.payload_from_saved(bad)
            self.assertIsNone(payload, bad)
            self.assertTrue(why)

    def run_cli(self, *args: str, expect_code: int = 0) -> dict:
        p = subprocess.run([sys.executable, str(VIZ), *args],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, expect_code,
                         f"exit {p.returncode}: {p.stdout} {p.stderr}")
        return json.loads(p.stdout)

    def test_cli_rerenders_report_from_saved_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved = Path(tmp) / "result.json"
            report = Path(tmp) / "report.html"
            saved.write_text(json.dumps(saved_predecessors()),
                             encoding="utf-8")
            out = self.run_cli(str(saved), "--html", str(report))
            self.assertTrue(out["ok"])
            self.assertEqual(out["mode"], "render")
            self.assertEqual(out["source_mode"], "predecessors")
            self.assertEqual(out["queries_total"], 1)
            self.assertFalse(out["graph_embedded"])
            self.assertTrue(any("그래프 미포함" in n for n in out["notes"]))
            # 회사 AI 정책 — stdout JSON에 로컬 경로 없음
            self.assertNotIn(tmp, json.dumps(out))

            html = report.read_text(encoding="utf-8")
            m = re.search(
                r'<script id="data" type="application/json">(.*?)</script>',
                html, re.S)
            data = json.loads(m.group(1))
            # 질의별 상세는 저장본 그대로 — 그래프 없음은 명시적 null
            self.assertEqual(data["queries"],
                             saved_predecessors()["queries"])
            self.assertIsNone(data["graph"])
            self.assertTrue(data["regenerated"])

    def test_cli_rejects_digest_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved = Path(tmp) / "digest.json"
            saved.write_text(json.dumps(
                {**saved_predecessors(), "output_written": True}),
                encoding="utf-8")
            out = self.run_cli(str(saved), "--html", str(Path(tmp) / "r.html"),
                               expect_code=2)
            self.assertEqual(out["error_code"], "INVALID_ARGUMENT")

    def test_cli_unreadable_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = self.run_cli(str(Path(tmp) / "no-such.json"),
                               "--html", str(Path(tmp) / "r.html"),
                               expect_code=3)
            self.assertEqual(out["error_code"], "INPUT_FILE_UNREADABLE")

    def test_template_handles_absent_graph(self):
        """그래프 미포함 상태를 브라우저 JS가 명시적으로 다룬다."""
        self.assertIn("DATA.graph != null", predecessors_viz.HTML_TEMPLATE)
        self.assertIn("공유 그래프 미포함", predecessors_viz.HTML_TEMPLATE)


if __name__ == "__main__":
    unittest.main()
