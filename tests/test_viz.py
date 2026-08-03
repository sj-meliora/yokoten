"""predecessors_viz.py 회귀 테스트 — git 없이 순수 렌더링 계층만 검증.

CLI를 통한 end-to-end 리포트 생성은 test_predecessors.py가 검증한다.
여기서는 모듈 경계(분석과 시각화의 분리)가 지켜지는지와 payload → HTML
변환 계약만 본다.
"""

import json
import re
import tempfile
import unittest
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import predecessors_viz


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


if __name__ == "__main__":
    unittest.main()
