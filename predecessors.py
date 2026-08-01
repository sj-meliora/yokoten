#!/usr/bin/env python3
"""predecessors.py — 주어진 FTL sha에 선행하는 미횡전개 커밋 탐지.

yokoten(횡전개(橫展開) 지원 도구 모음)의 스크립트. excel에는 FTL 기준 sha만
적혀 있는데, 그 커밋이 의존하는 선행 커밋이 목록에서 누락됐을 수 있다. 이
스크립트는 주어진 FTL 커밋 F에 대해 "source branch 이력에서 F의 ancestor이면서
아직 target branch(예: origin/develop)에 횡전개되지 않은 커밋"을 찾아, F만
단독으로 cherry-pick하면 충돌하거나 조용히 깨질 수 있는 상황을 사전에 드러낸다.

판정 원리:

1. **미반영 후보 추출** — 횡전개는 cherry-pick이라 target에는 다른 sha로
   존재하므로 ancestry만으로는 반영 여부를 알 수 없다. patch 등가
   (`rev-list --right-only --cherry-pick T...F`)로 "target에 패치 등가물이
   없는 F의 ancestor"만 남긴다. merge 커밋은 patch 등가 판정이 불가해 목록에서
   제외하고 건수(`merges_skipped`)만 보고한다.
2. **IMS key 2차 판정** — 충돌 해소·squash로 패치가 변형된 pick은 patch-id가
   어긋나 거짓 미반영이 된다. 커밋 메시지의 IMS key(예: AGCD-134)는 횡전개 시
   유지되므로, target 쪽 커밋 메시지에서 같은 key가 발견되면
   `applied_evidence: "ims_key"`(변형 반영 가능성 — 사람이 확인)로 표시한다.
   key 하나가 커밋 여러 개에 걸칠 수 있으므로 자동 제외하지는 않는다.
3. **위험도 분류** — 선행 커밋의 변경 파일이 F의 변경 파일과 겹치면
   `required_first`(먼저 pick하지 않으면 충돌·의존 파손 가능성 높음), 아니면
   `independent`(topological 선행이지만 독립일 수 있음 — 정보성).
4. **배달 pegging 버킷팅** — resolve_sha.Resolver를 재사용해 각 선행 커밋이
   어느 pegging으로 배달됐는지, F와 같은 batch인지, 그 pegging에서 다른
   gitlink(HAL/Shared/FIL)가 함께 움직였는지(`companions_moved`)를 표시한다.
   동반 커밋의 상세 목록·세트 구성은 resolve_sha.py로 후속 조회한다.
5. **HTML 리포트(`--html PATH`)** — 판정 결과와 전체 질의 구간 합집합의
   커밋 그래프 한 벌(부모 edge + 커밋별 반영 상태)을 내장한 self-contained
   HTML을 쓴다. 상단 요약 타일로 triage하고, "미반영 커밋 통합 뷰"에서 각
   미반영 커밋이 몇 개의 질의를 막는지(blocking) 본 뒤, 커밋 클릭으로
   조상 반영 여부를 드릴다운한다. 그래프가 공유라 파일 크기는 질의 수와
   거의 무관하다. 외부 리소스(CDN 등)는 쓰지 않으며, stdout JSON 계약은
   변하지 않는다(로컬 경로도 싣지 않는다).

회사 AI 정책에 따라 출력에 author 등 개발자 식별 정보는 싣지 않는다
(sha·날짜·제목·IMS key만). stdout JSON에는 remote URL·repo 경로·git stderr를
싣지 않는다.

exit code: 0=성공 (개별 sha의 실패는 queries[].status로 보고) /
2=인자·검증 오류 / 3=repo 접근 오류
"""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from resolve_sha import (HEX_RE, FetchReport, JsonArgumentParser, Resolver,
                         commit_meta, emit, fail, git, is_ancestor,
                         is_git_repo, read_input_file, resolve_commit)

DEFAULT_IMS_PATTERN = r"\b[A-Z][A-Z0-9]+-\d+\b"
MAX_GRAPH_NODES = 2000  # HTML 리포트에 내장하는 구간 그래프 노드 상한


def rev_list(repo: Path, *args: str) -> list[str] | None:
    rc, out, _ = git(repo, "rev-list", *args)
    return out.splitlines() if rc == 0 else None


def changed_paths(repo: Path, sha: str) -> set[str] | None:
    rc, out, _ = git(repo, "diff-tree", "--no-commit-id", "--name-only",
                     "-r", "--root", sha)
    if rc != 0:
        return None
    return {line for line in out.splitlines() if line}


def is_merge(repo: Path, sha: str) -> bool:
    return git(repo, "rev-parse", "--verify", "--quiet", sha + "^2")[0] == 0


class PredecessorScanner:
    """target 대비 미반영 선행 커밋을 판정한다. key·pegging 조회는 캐시."""

    def __init__(self, rs: Resolver, target_sha: str, ims: re.Pattern,
                 limit: int, collect_graph: bool = False):
        self.rs = rs
        self.target = target_sha
        self.ims = ims
        self.limit = limit
        self.collect_graph = collect_graph
        self._all_side: set[str] = set()   # 공유 그래프용 patch 판정 누적
        self._missing: set[str] = set()
        self._keys: dict[str, list[str]] = {}
        self._target_keys: dict[str, set[str]] = {}
        self._moved: dict[int, list[str] | None] = {}

    def message_keys(self, sha: str) -> list[str]:
        if sha not in self._keys:
            rc, out, _ = git(self.rs.ftl, "show", "-s", "--format=%B", sha)
            self._keys[sha] = \
                sorted(set(self.ims.findall(out))) if rc == 0 else []
        return self._keys[sha]

    def target_keys(self, ftl_sha: str) -> set[str]:
        """target 쪽(F 미도달 구간) 커밋 메시지에 등장하는 IMS key 집합.

        key마다 git log --grep을 도는 대신 구간 메시지를 한 번에 읽는다 —
        key 추출 규칙이 양쪽에 동일하게 적용되므로 부분 일치 문제도 없다.
        """
        if ftl_sha not in self._target_keys:
            rc, out, _ = git(self.rs.ftl, "log", "-z", "--format=%B",
                             f"{ftl_sha}..{self.target}")
            self._target_keys[ftl_sha] = \
                set(self.ims.findall(out)) if rc == 0 else set()
        return self._target_keys[ftl_sha]

    def applied_evidence(self, sha: str, ftl_sha: str) -> str:
        if set(self.message_keys(sha)) & self.target_keys(ftl_sha):
            return "ims_key"
        return "none"

    def moved_paths(self, idx: int) -> list[str] | None:
        """pegging에서 FTL 외에 움직인 gitlink 경로 (부모 없음 등은 None)."""
        if idx in self._moved:
            return self._moved[idx]
        rs = self.rs
        pegging = rs.peggings[idx]
        paths = None
        rc, parent, _ = git(rs.integ, "rev-parse", "--verify", "--quiet",
                            pegging + "^")
        if rc == 0:
            rc, out, _ = git(rs.integ, "diff-tree", "--no-renames", "-r",
                             "--raw", parent, pegging)
            if rc == 0:
                paths = []
                for line in out.splitlines():
                    if not line.startswith(":"):
                        continue
                    front, _, path = line.partition("\t")
                    old_mode, new_mode = front[1:].split()[:2]
                    if "160000" in (old_mode, new_mode) and path != rs.subpath:
                        paths.append(path)
        self._moved[idx] = paths
        return paths

    def scan(self, q: dict, full: str, f_idx: int | None) -> None:
        rs = self.rs
        spec = f"{self.target}...{full}"
        all_side = rev_list(rs.ftl, "--right-only", "--no-merges", spec)
        missing = rev_list(rs.ftl, "--right-only", "--cherry-pick",
                           "--no-merges", spec)
        merges = rev_list(rs.ftl, "--right-only", "--merges", spec)
        if all_side is None or missing is None or merges is None:
            q["notes"].append("선행 커밋 열거 실패 — object가 없으면 --fetch 후 재시도")
            return
        missing_set = set(missing)
        merge_self = is_merge(rs.ftl, full)

        # F 자신의 target 반영 상태 — "이 sha는 이미 횡전개됨" 신호
        if is_ancestor(rs.ftl, full, self.target):
            applied = "in_target_history"
        elif merge_self:
            applied = "unknown"
            q["notes"].append("조회 sha가 merge 커밋 — patch 등가 판정 불가")
        elif full not in missing_set:
            applied = "patch_applied"
        elif self.applied_evidence(full, full) == "ims_key":
            applied = "key_matched"
        else:
            applied = "not_applied"
        q["self"] = {"applied": applied, "ims_keys": self.message_keys(full)}

        f_paths = None if merge_self else changed_paths(rs.ftl, full)
        cand = [c for c in reversed(missing) if c != full]  # 오래된 순
        total = len(cand)
        truncated = bool(self.limit) and total > self.limit
        if truncated:
            cand = cand[:self.limit]

        preds = []
        for c in cand:
            c_idx = rs.locate_ancestor(c)
            c_paths = changed_paths(rs.ftl, c)
            overlap = sorted(f_paths & c_paths) \
                if f_paths is not None and c_paths is not None else None
            if overlap is None:
                risk = "unknown"
            elif overlap:
                risk = "required_first"
            else:
                risk = "independent"
            same_batch = (c_idx == f_idx) \
                if c_idx is not None and f_idx is not None else None
            preds.append({
                **commit_meta(rs.ftl, c),
                "pegging": rs.peggings[c_idx][:7] if c_idx is not None else None,
                "same_batch": same_batch,
                "ims_keys": self.message_keys(c),
                "applied_evidence": self.applied_evidence(c, full),
                "risk": risk,
                "overlap_paths": overlap,
                "companions_moved":
                    self.moved_paths(c_idx) if c_idx is not None else None,
            })

        q.update({
            "predecessors": preds,
            "predecessors_total": total,
            "predecessors_truncated": truncated,
            "applied_total":
                len(set(all_side) - {full}) - len(missing_set - {full}),
            "merges_skipped": len([m for m in merges if m != full]),
        })
        if self.collect_graph:
            self._all_side.update(all_side)
            self._missing.update(missing_set)

    def build_graph(self, fulls: list[str]) -> dict:
        """HTML 리포트용 공유 커밋 그래프 — 모든 질의 구간의 합집합 한 벌.

        질의마다 T...F 그래프를 따로 내장하면 같은 branch의 sha 여러 개에서
        거의 같은 그래프가 중복돼 파일이 질의 수에 비례해 커진다. 반영 상태
        판정은 질의와 무관하게 target 기준이므로 그래프는 한 벌만 내장하고
        각 질의는 시작 커밋(ftl_sha)만 가리킨다. --limit으로 잘린
        predecessors 목록과 무관하게 MAX_GRAPH_NODES까지 담는다 (topo 순 —
        브라우저가 오래된 순 정렬에 그대로 사용).
        """
        if not fulls:
            return {"nodes": [], "truncated": False}
        rc, out, _ = git(self.rs.ftl, "log", "-z", "--topo-order",
                         "--format=%H%x1f%P%x1f%cs%x1f%s%x1f%B",
                         *fulls, "--not", self.target)
        if rc != 0:
            return {"nodes": [], "truncated": False}
        # 공유 target-side key 집합 — 어느 질의 구간에서도 도달 못 하는
        # target 커밋들의 메시지에서 추출
        rc, tout, _ = git(self.rs.ftl, "log", "-z", "--format=%B",
                          self.target, "--not", *fulls)
        tkeys = set(self.ims.findall(tout)) if rc == 0 else set()
        records = [r for r in out.split("\0") if r]
        nodes = []
        for rec in records[:MAX_GRAPH_NODES]:
            sha, parents, date, subject, body = rec.split("\x1f", 4)
            plist = parents.split()
            keys = sorted(set(self.ims.findall(body)))
            self._keys.setdefault(sha, keys)
            if len(plist) > 1:
                status = "merge"
            elif sha in self._missing:
                status = "key_matched" if set(keys) & tkeys else "not_applied"
            elif sha in self._all_side:
                status = "patch_applied"
            else:
                status = "unknown"  # 해당 질의의 구간 열거가 실패한 경우
            nodes.append({"sha": sha, "short": sha[:7], "parents": plist,
                          "date": date, "subject": subject,
                          "ims_keys": keys, "status": status})
        return {"nodes": nodes, "truncated": len(records) > MAX_GRAPH_NODES}


# ------------------------------------------------------------------ HTML 리포트

# self-contained 리포트 — 외부 리소스(CDN·폰트·이미지) 없이 inline CSS/JS만
# 사용한다. 데이터는 <script type="application/json">에 내장하고, 조상 탐색은
# 브라우저에서 부모 edge를 따라 수행한다 (클릭마다 git 재실행 불필요).
HTML_TEMPLATE = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>yokoten — 선행 커밋 리포트</title>
<style>
:root{
  --bg:#ffffff;--fg:#1c2733;--muted:#5c6b7a;--line:#dde4ea;--card:#f6f8fa;
  --red:#b42318;--red-bg:#fee4e2;--amber:#93500b;--amber-bg:#fdefd4;
  --green:#067647;--green-bg:#d9f2e5;--gray:#475467;--gray-bg:#e8ecf1;
  --accent:#175cd3;--accent-bg:#e3ecfb;
}
@media (prefers-color-scheme: dark){
  :root{--bg:#10161d;--fg:#e6edf3;--muted:#9aa7b4;--line:#2c3947;--card:#161f29;
   --red:#f97066;--red-bg:#3b1a18;--amber:#f7b25c;--amber-bg:#3a2a12;
   --green:#5cc489;--green-bg:#12301f;--gray:#b0bac4;--gray-bg:#242e39;
   --accent:#6ba6f8;--accent-bg:#16283f;}
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
     font:14px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
header{padding:20px 24px;border-bottom:1px solid var(--line)}
h1{margin:0 0 8px;font-size:18px}
#meta{color:var(--muted);font-size:12px}
#meta b{color:var(--fg);font-weight:600}
main{max-width:1080px;padding:16px 24px 80px}
.tiles{display:flex;flex-wrap:wrap;gap:10px;margin:14px 0 4px}
.tile{border:1px solid var(--line);border-radius:8px;padding:8px 14px;
      cursor:pointer;background:var(--card);min-width:104px}
.tile b{display:block;font-size:20px;font-variant-numeric:tabular-nums}
.tile span{font-size:11px;color:var(--muted)}
.tile.active{outline:2px solid var(--accent)}
.tile.t-red b{color:var(--red)} .tile.t-amber b{color:var(--amber)}
.tile.t-green b{color:var(--green)} .tile.t-gray b{color:var(--gray)}
.filterbar{display:flex;gap:10px;margin:12px 0;align-items:center}
.filterbar input{flex:1;max-width:420px;padding:6px 10px;font:inherit;
  border:1px solid var(--line);border-radius:6px;background:var(--bg);
  color:var(--fg)}
.filterbar input:focus{outline:2px solid var(--accent)}
h2.sect{font-size:14px;margin:26px 0 8px}
.peg-group{margin:18px 0 6px;color:var(--muted);font-size:12px;
  border-bottom:1px solid var(--line);padding-bottom:4px}
details.query{margin:10px 0;border:1px solid var(--line);border-radius:8px;
              overflow:hidden}
summary.qhead{list-style:none;padding:12px 16px;background:var(--card);
  display:flex;flex-wrap:wrap;gap:8px;align-items:center;cursor:pointer}
summary.qhead::-webkit-details-marker{display:none}
summary.qhead:hover{background:var(--accent-bg)}
details[open]>summary.qhead{border-bottom:1px solid var(--line)}
summary.qhead .caret{color:var(--muted);font-size:11px;transition:transform .12s}
details[open]>summary.qhead .caret{transform:rotate(90deg)}
@media (prefers-reduced-motion: reduce){summary.qhead .caret{transition:none}}
.qhead .sha{font-weight:700;color:var(--accent);cursor:pointer}
.qhead .sha:hover{text-decoration:underline}
.qsub{width:100%;color:var(--muted);font-size:12px}
.blocks{white-space:nowrap;font-variant-numeric:tabular-nums}
.blocks .d{font-size:11px}
section.unified{border:1px solid var(--line);border-radius:8px;overflow:hidden}
.badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;
       white-space:nowrap}
.b-red{color:var(--red);background:var(--red-bg)}
.b-amber{color:var(--amber);background:var(--amber-bg)}
.b-green{color:var(--green);background:var(--green-bg)}
.b-gray{color:var(--gray);background:var(--gray-bg)}
.b-accent{color:var(--accent);background:var(--accent-bg)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{color:var(--muted);font-weight:600;font-size:11px;text-align:left}
th,td{padding:7px 10px;border-top:1px solid var(--line);vertical-align:top}
tr.commit{cursor:pointer}
tr.commit:hover{background:var(--accent-bg)}
td.sha{white-space:nowrap;color:var(--accent)}
td.date{white-space:nowrap;color:var(--muted)}
td.keys{white-space:nowrap}
.empty{padding:14px 16px;color:var(--muted)}
.warn{margin:8px 0;padding:8px 12px;border:1px solid var(--amber);
      border-radius:6px;color:var(--amber);background:var(--amber-bg);font-size:12px}
#panel{position:fixed;top:0;right:0;bottom:0;width:min(460px,90vw);
       background:var(--bg);border-left:1px solid var(--line);
       box-shadow:-6px 0 24px rgba(0,0,0,.15);overflow-y:auto;padding:16px 20px}
#panel[hidden]{display:none}
#panel h2{font-size:14px;margin:4px 0 2px;word-break:break-all}
#panel .close{float:right;border:1px solid var(--line);background:var(--card);
              color:var(--fg);border-radius:6px;padding:2px 10px;cursor:pointer}
#panel .group{margin-top:14px}
#panel .group>h3{font-size:12px;color:var(--muted);margin:0 0 6px}
.anc{display:flex;flex-wrap:wrap;gap:4px 8px;padding:5px 6px;border-radius:6px;
     cursor:pointer;align-items:baseline}
.anc:hover{background:var(--accent-bg)}
.anc .s{color:var(--accent);white-space:nowrap}
.anc .d{color:var(--muted);white-space:nowrap;font-size:12px}
.anc .t{overflow-wrap:anywhere}
details{margin-top:6px}
summary{cursor:pointer;color:var(--muted);font-size:12px}
.kv{font-size:12px;color:var(--muted);margin:2px 0}
.kv b{color:var(--fg)}
</style>
</head>
<body>
<header>
  <h1>yokoten — 선행 커밋 리포트</h1>
  <div id="meta"></div>
</header>
<main id="queries"></main>
<aside id="panel" hidden></aside>
<script id="data" type="application/json">__DATA__</script>
<script>
"use strict";
const DATA = JSON.parse(document.getElementById("data").textContent);
const STATUS = {
  not_applied:   ["미반영", "b-red"],
  key_matched:   ["key 일치 — 확인 필요", "b-amber"],
  patch_applied: ["반영됨 (patch 등가)", "b-green"],
  merge:         ["merge — 판정 불가", "b-gray"],
  unknown:       ["판정 불가", "b-gray"],
};
const QCLASS = {
  danger: ["미반영 선행 있음", "t-red"],
  review: ["확인 필요", "t-amber"],
  clean:  ["단독 pick 가능", "t-green"],
  done:   ["이미 반영됨", "t-gray"],
  error:  ["FTL에 없음", "t-red"],
};
const SELF_APPLIED = {
  not_applied:       ["미반영", "b-red"],
  key_matched:       ["key 일치 — 확인 필요", "b-amber"],
  patch_applied:     ["반영됨 (patch 등가)", "b-green"],
  in_target_history: ["target 이력에 포함", "b-green"],
  unknown:           ["판정 불가", "b-gray"],
};
const RISK = {
  required_first: ["required_first", "b-red"],
  independent:    ["independent", "b-gray"],
  unknown:        ["risk 판정 불가", "b-gray"],
};

// 공유 그래프 — 모든 질의 구간의 합집합 한 벌 (topo 순, 0 = 최신)
const G = (() => {
  const map = new Map(), order = new Map();
  ((DATA.graph || {}).nodes || []).forEach((n, i) => {
    map.set(n.sha, n); order.set(n.sha, i);
  });
  return { map, order, truncated: (DATA.graph || {}).truncated };
})();
const queried = new Set(DATA.queries.map(q => q.ftl_sha).filter(Boolean));
// 질의별 조상 집합 — 미반영 커밋의 blocking count 계산용
const qAnc = new Map();
for (const q of DATA.queries) {
  if (!q.ftl_sha || !G.map.has(q.ftl_sha)) continue;
  const seen = new Set(), stack = [...G.map.get(q.ftl_sha).parents];
  while (stack.length) {
    const s = stack.pop();
    if (seen.has(s)) continue;
    seen.add(s);
    const n = G.map.get(s);
    if (n) stack.push(...n.parents);
  }
  qAnc.set(q.input, seen);
}
function blockedQueries(sha) {
  return DATA.queries.filter(q => qAnc.has(q.input) && qAnc.get(q.input).has(sha));
}
function qClass(q) {
  if (q.status === "not_found_in_ftl") return "error";
  const s = q.self && q.self.applied;
  if (s === "patch_applied" || s === "in_target_history") return "done";
  const preds = q.predecessors || [];
  if (preds.some(p => p.applied_evidence === "none")) return "danger";
  if (preds.length || s === "key_matched") return "review";
  return "clean";
}
const FILTER = { cls: null, text: "" };

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}
function badge(pair) { return el("span", "badge " + pair[1], pair[0]); }

function ancestorsOf(sha) {
  if (!G.map.has(sha)) return null;
  const seen = new Set(), stack = [...G.map.get(sha).parents];
  const inRegion = []; let boundary = 0;
  while (stack.length) {
    const s = stack.pop();
    if (seen.has(s)) continue;
    seen.add(s);
    const n = G.map.get(s);
    if (!n) { boundary++; continue; }   // 구간 밖 = target 반영 완료 이력
    inRegion.push(n);
    stack.push(...n.parents);
  }
  // 내장 순서는 최신순 — 뒤집어 오래된 순(pick 적용 순서)으로
  inRegion.sort((a, b) => G.order.get(b.sha) - G.order.get(a.sha));
  return { inRegion, boundary };
}

function ancRow(n) {
  const row = el("div", "anc");
  row.append(el("span", "s", n.short), el("span", "d", n.date || ""),
             badge(STATUS[n.status] || [n.status, "b-gray"]),
             el("span", "t", n.subject || ""));
  row.onclick = () => showCommit(n.sha);
  return row;
}

function group(panel, title, items, open) {
  if (!items.length) return;
  const g = el("div", "group");
  if (open) {
    g.append(el("h3", null, title + " (" + items.length + ")"));
    items.forEach(n => g.append(ancRow(n)));
  } else {
    const d = el("details");
    d.append(el("summary", null, title + " (" + items.length + ")"));
    items.forEach(n => d.append(ancRow(n)));
    g.append(d);
  }
  panel.append(g);
}

function showCommit(sha) {
  const panel = document.getElementById("panel");
  panel.hidden = false;
  panel.replaceChildren();
  const close = el("button", "close", "닫기");
  close.onclick = () => { panel.hidden = true; };
  panel.append(close);

  const n = G.map.get(sha);
  if (!n) { panel.append(el("p", "empty", "그래프에 없는 커밋")); return; }

  panel.append(el("h2", null, n.short + "  " + (n.subject || "")));
  const info = el("div");
  info.append(el("div", "kv", "sha: " + n.sha),
              el("div", "kv", "date: " + (n.date || "?")));
  if (n.ims_keys.length)
    info.append(el("div", "kv", "IMS key: " + n.ims_keys.join(", ")));
  const st = el("div", "kv");
  st.append("상태: ", badge(STATUS[n.status] || [n.status, "b-gray"]));
  if (queried.has(sha)) st.append(" ", badge(["질의 대상", "b-accent"]));
  info.append(st);

  // 이 커밋을 선행으로 갖는 질의별 risk — 질의마다 겹침 파일이 다르다
  for (const q of DATA.queries) {
    const pred = (q.predecessors || []).find(p => p.sha === sha);
    if (!pred) continue;
    const extra = el("div", "kv");
    extra.append("질의 " + (q.ftl_short || q.input) + ": ",
                 badge(RISK[pred.risk] || [pred.risk, "b-gray"]));
    if ((pred.overlap_paths || []).length)
      extra.append(" " + pred.overlap_paths.join(", "));
    if (pred.pegging)
      extra.append(" · pegging " + pred.pegging
                   + (pred.same_batch ? " (같은 batch)" : ""));
    if ((pred.companions_moved || []).length)
      extra.append(" · 동반 " + pred.companions_moved.join(", "));
    info.append(extra);
  }
  if (n.status === "not_applied" || n.status === "key_matched") {
    const blocked = blockedQueries(sha);
    if (blocked.length)
      info.append(el("div", "kv", "이 커밋이 막는 질의: " + blocked.length
        + "건 — " + blocked.map(q => q.ftl_short || q.input).join(", ")));
  }
  panel.append(info);

  const anc = ancestorsOf(sha);
  const h = el("div", "group");
  h.append(el("h3", null, "조상 커밋 (오래된 순)"));
  panel.append(h);
  if (G.truncated)
    panel.append(el("div", "warn",
      "그래프가 " + DATA.max_graph_nodes + "개 노드에서 절단됨 — 조상 목록이 불완전할 수 있음"));
  if (!anc.inRegion.length && !anc.boundary) {
    panel.append(el("p", "empty", "구간 내 조상 없음"));
    return;
  }
  const by = s => anc.inRegion.filter(x => x.status === s);
  group(panel, "미반영 — 먼저 횡전개 필요", by("not_applied"), true);
  group(panel, "key 일치 — 반영 여부 확인 필요", by("key_matched"), true);
  group(panel, "merge — 판정 불가", by("merge"), false);
  group(panel, "반영됨 (patch 등가)", by("patch_applied"), false);
  group(panel, "판정 불가", by("unknown"), false);
  if (anc.boundary)
    panel.append(el("div", "kv",
      "이하 조상은 target 반영 완료 이력에 도달 (경계 부모 " + anc.boundary + "개)"));
}

let searchBox = null;

function applyFilter() {
  const t = (searchBox ? searchBox.value : "").trim().toLowerCase();
  document.querySelectorAll("details.query").forEach(d => {
    const okC = !FILTER.cls || d.dataset.cls === FILTER.cls;
    const okT = !t || d.dataset.hay.includes(t);
    d.style.display = okC && okT ? "" : "none";
  });
  // 그룹 헤더는 보이는 질의가 하나도 없으면 함께 숨긴다
  let header = null, visible = false;
  const flush = () => { if (header) header.style.display = visible ? "" : "none"; };
  for (const child of document.getElementById("queries").children) {
    if (child.classList.contains("peg-group")) {
      flush(); header = child; visible = false;
    } else if (child.classList.contains("query") && child.style.display !== "none") {
      visible = true;
    }
  }
  flush();
}

function buildQuery(q) {
  const cls = qClass(q);
  const sec = el("details", "query");
  sec.dataset.cls = cls;
  sec.dataset.hay = [q.input, q.ftl_sha || "",
    ...(q.self ? q.self.ims_keys : []),
    ...(q.predecessors || []).flatMap(p => [p.sha, p.subject || "",
                                            ...p.ims_keys])].join(" ").toLowerCase();
  if (cls === "danger" || cls === "review" || cls === "error") sec.open = true;

  const head = el("summary", "qhead");
  head.append(el("span", "caret", "▶"));
  const shaEl = el("span", "sha", q.ftl_short || q.input);
  if (q.ftl_sha) {
    shaEl.title = "커밋 드릴다운";
    shaEl.onclick = e => { e.preventDefault(); e.stopPropagation();
                           showCommit(q.ftl_sha); };
  }
  head.append(shaEl);
  head.append(badge([QCLASS[cls][0], "b-" + QCLASS[cls][1].slice(2)]));
  if (q.status === "not_pegged") head.append(badge(["not_pegged", "b-amber"]));
  if (q.pegging) head.append(badge(["pegging " + q.pegging, "b-accent"]));
  if (q.self && q.self.applied === "key_matched")
    head.append(badge(SELF_APPLIED.key_matched));
  const sub = el("div", "qsub");
  if (q.self && q.self.ims_keys.length)
    sub.append("IMS key: " + q.self.ims_keys.join(", ") + " · ");
  if (q.predecessors !== null)
    sub.append("미반영 선행 " + q.predecessors_total + "건 · patch 등가 반영 "
               + q.applied_total + "건 · merge 제외 " + q.merges_skipped + "건");
  head.append(sub);
  sec.append(head);

  for (const note of q.notes || [])
    sec.append(el("div", "warn", note));

  if (q.predecessors === null) {
    sec.append(el("p", "empty", "선행 커밋 판정 없음"));
  } else if (!q.predecessors.length) {
    sec.append(el("p", "empty", "미반영 선행 커밋 없음 — 단독 pick 가능 (patch 등가 기준)"));
  } else {
    if (q.predecessors_truncated)
      sec.append(el("div", "warn", "목록이 --limit에서 절단됨 (전체 "
                    + q.predecessors_total + "건) — 그래프 클릭으로는 전부 탐색 가능"));
    const tb = el("table"), thead = el("thead"), tr = el("tr");
    for (const h of ["sha", "date", "subject", "IMS key", "pegging", "risk", "판정"])
      tr.append(el("th", null, h));
    thead.append(tr); tb.append(thead);
    const body = el("tbody");
    for (const p of q.predecessors) {
      const r = el("tr", "commit");
      r.append(el("td", "sha", p.short), el("td", "date", p.date || ""),
               el("td", null, p.subject || ""),
               el("td", "keys", p.ims_keys.join(", ")));
      const peg = el("td");
      if (p.pegging) {
        peg.append(p.pegging);
        if (p.same_batch) peg.append(" ", badge(["같은 batch", "b-amber"]));
        if ((p.companions_moved || []).length)
          peg.append(" ", badge(["동반 " + p.companions_moved.join(","), "b-accent"]));
      } else peg.append("—");
      r.append(peg);
      const risk = el("td");
      risk.append(badge(RISK[p.risk] || [p.risk, "b-gray"]));
      if ((p.overlap_paths || []).length)
        risk.append(" ", el("span", "d", p.overlap_paths.join(", ")));
      r.append(risk);
      const ev = el("td");
      ev.append(badge(p.applied_evidence === "ims_key"
                      ? STATUS.key_matched : STATUS.not_applied));
      r.append(ev);
      r.onclick = () => showCommit(p.sha);
      body.append(r);
    }
    tb.append(body);
    sec.append(tb);
  }
  return sec;
}

function render() {
  const meta = document.getElementById("meta");
  // 정적 마크업만 innerHTML로 넣고 데이터는 textContent로 채운다
  meta.innerHTML =
    "source <b></b> → target <b></b> · submodule <b></b> · 생성 <b></b>";
  const bs = meta.querySelectorAll("b");
  bs[0].textContent = DATA.branch + " @ " + DATA.branch_tip.short;
  bs[1].textContent = DATA.target.ref + " @ " + DATA.target.short;
  bs[2].textContent = DATA.submodule;
  bs[3].textContent = DATA.generated;

  const root = document.getElementById("queries");

  // 요약 타일 — triage 순서: 클릭하면 해당 상태만 필터
  const counts = { danger: 0, review: 0, clean: 0, done: 0, error: 0 };
  DATA.queries.forEach(q => counts[qClass(q)]++);
  const tiles = el("div", "tiles");
  const addTile = (cls, label, count, tone) => {
    const t = el("div", "tile " + tone);
    t.append(el("b", null, String(count)), el("span", null, label));
    t.onclick = () => {
      FILTER.cls = cls;
      document.querySelectorAll(".tile").forEach(x =>
        x.classList.toggle("active", x === t));
      applyFilter();
    };
    tiles.append(t);
    return t;
  };
  const all = addTile(null, "질의 전체", DATA.queries.length, "t-gray");
  all.classList.add("active");
  for (const [cls, [label, tone]] of Object.entries(QCLASS))
    if (counts[cls]) addTile(cls, label, counts[cls], tone);
  root.append(tiles);

  const bar = el("div", "filterbar");
  searchBox = document.createElement("input");
  searchBox.type = "search";
  searchBox.placeholder = "sha · 제목 · IMS key 검색";
  searchBox.oninput = applyFilter;
  bar.append(searchBox);
  root.append(bar);

  // 미반영 커밋 통합 뷰 — 오래된 순 = 그대로 pick 작업 순서
  root.append(el("h2", "sect", "미반영 커밋 통합 뷰 (pick 적용 순서)"));
  if (G.truncated)
    root.append(el("div", "warn", "그래프가 " + DATA.max_graph_nodes
                 + "개 노드에서 절단됨 — 통합 뷰가 불완전할 수 있음"));
  const blockers = [...G.map.values()]
    .filter(n => n.status === "not_applied" || n.status === "key_matched")
    .sort((a, b) => G.order.get(b.sha) - G.order.get(a.sha));
  if (!blockers.length) {
    root.append(el("p", "empty", "미반영 커밋 없음"));
  } else {
    const uni = el("section", "unified");
    const tb = el("table"), thead = el("thead"), tr = el("tr");
    for (const h of ["sha", "date", "subject", "IMS key", "판정", "막는 질의"])
      tr.append(el("th", null, h));
    thead.append(tr); tb.append(thead);
    const body = el("tbody");
    for (const n of blockers) {
      const r = el("tr", "commit");
      r.append(el("td", "sha", n.short), el("td", "date", n.date || ""));
      const subj = el("td", null, n.subject || "");
      if (queried.has(n.sha)) subj.append(" ", badge(["질의 대상", "b-accent"]));
      r.append(subj, el("td", "keys", n.ims_keys.join(", ")));
      const st = el("td");
      st.append(badge(STATUS[n.status]));
      r.append(st);
      const blocked = blockedQueries(n.sha);
      const bcell = el("td", "blocks");
      if (blocked.length) {
        bcell.append(blocked.length + "건 ");
        const shorts = blocked.map(q => q.ftl_short || q.input);
        bcell.append(el("span", "d", shorts.slice(0, 4).join(", ")
                     + (shorts.length > 4 ? " 외 " + (shorts.length - 4) : "")));
      } else bcell.append("—");
      r.append(bcell);
      r.onclick = () => showCommit(n.sha);
      body.append(r);
    }
    tb.append(body);
    uni.append(tb);
    root.append(uni);
  }

  // 질의별 상세 — 오래된 순(배달 순서), 같은 pegging끼리 그룹
  root.append(el("h2", "sect", "질의별 상세"));
  const qs = [...DATA.queries].sort((a, b) => {
    const oa = a.ftl_sha && G.order.has(a.ftl_sha) ? G.order.get(a.ftl_sha) : -1;
    const ob = b.ftl_sha && G.order.has(b.ftl_sha) ? G.order.get(b.ftl_sha) : -1;
    return ob - oa;  // order 내림차순 = 오래된 순, 해석 불가(-1)는 마지막
  });
  let lastGroup = null;
  for (const q of qs) {
    const label = q.pegging ? "pegging " + q.pegging
      : q.status === "not_pegged" ? "미배달 (not_pegged)" : "해석 불가";
    if (label !== lastGroup) {
      root.append(el("div", "peg-group", label));
      lastGroup = label;
    }
    root.append(buildQuery(q));
  }
}
render();
</script>
</body>
</html>
"""


def write_report(path: str, payload: dict) -> str | None:
    """리포트 파일 쓰기. 실패 사유 문자열 반환 (성공 시 None)."""
    data = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    # </script> 조기 종료 방지 — JSON 문자열 안의 "</"를 이스케이프
    html = HTML_TEMPLATE.replace("__DATA__", data.replace("</", "<\\/"))
    try:
        Path(path).write_text(html, encoding="utf-8")
    except OSError:
        return "리포트 파일을 쓸 수 없음 — 경로·권한 확인"
    return None


# ------------------------------------------------------------------ CLI

def cmd_predecessors(args) -> int:
    fetch = FetchReport(args.fetch)

    integ = Path(args.repo).resolve()
    if not is_git_repo(integ):
        return fail("INTEGRATION_REPOSITORY_INVALID", "integration repo 아님", 3,
                    fetch=fetch.payload())

    ftl = Path(args.ftl_repo).resolve() if args.ftl_repo else integ / args.submodule
    if not is_git_repo(ftl) or \
            git(ftl, "rev-parse", "--show-toplevel")[1] == \
            git(integ, "rev-parse", "--show-toplevel")[1]:
        return fail("FTL_REPOSITORY_INVALID",
                    "FTL repo 아님 — submodule 미초기화면 --ftl-repo로 지정", 3,
                    fetch=fetch.payload())

    try:
        ims = re.compile(args.ims_pattern)
    except re.error:
        return fail("INVALID_ARGUMENT",
                    f"--ims-pattern 정규식 오류: {args.ims_pattern!r}",
                    fetch=fetch.payload())

    # 판정 전에 integration·FTL(target ref 포함)을 함께 갱신한다 — resolve_sha와
    # 동일한 stale snapshot 방지 규칙 (하나라도 실패하면 판정하지 않는다).
    if args.fetch:
        failed = [label for label, repo in
                  {"integration": integ, "FTL": ftl}.items()
                  if not fetch.refresh(repo, label)]
        if failed:
            return fail("FETCH_FAILED",
                        "최신 상태 확인 실패 — stale snapshot 판정을 막기 위해 중단: "
                        + ", ".join(failed), 3, fetch=fetch.payload())

    inputs = list(args.shas)
    skipped = 0
    if args.input:
        try:
            file_tokens, skipped = read_input_file(args.input)
        except OSError:
            return fail("INPUT_FILE_UNREADABLE",
                        "--input 파일을 읽을 수 없음", 3, fetch=fetch.payload())
        inputs.extend(file_tokens)
    for tok in inputs:
        if not HEX_RE.match(tok):
            return fail("INVALID_ARGUMENT",
                        f"sha 형식 오류: {tok!r} (hex 7~40자리)",
                        fetch=fetch.payload())
    inputs = list(dict.fromkeys(inputs))  # 중복 제거 (순서 유지)
    if not inputs:
        return fail("INVALID_ARGUMENT",
                    "FTL sha가 없음 — 인자 또는 --input으로 지정",
                    fetch=fetch.payload())

    tip = resolve_commit(integ, args.branch, fetch)
    if tip is None:
        return fail("BRANCH_NOT_FOUND",
                    f"{args.branch!r} 해석 불가 — 사용자에게 확인한 source "
                    "브랜치인지 확인 (예: origin/develop 또는 "
                    "origin/develop_XXX)", fetch=fetch.payload())

    target_sha = resolve_commit(ftl, args.target, fetch)
    if target_sha is None:
        return fail("TARGET_NOT_FOUND",
                    f"{args.target!r} 해석 불가 — 사용자에게 확인한 FTL target "
                    "branch인지 확인 (예: origin/develop; FTL repo의 ref)",
                    fetch=fetch.payload())

    rs = Resolver(integ, ftl, args.branch, args.submodule, fetch,
                  args.limit, args.thorough, {})
    if skipped:
        rs.note(f"--input에서 sha가 아닌 줄 {skipped}건 무시 (헤더 등)")
    why = rs.load_peggings()
    if why:
        return fail("PEGGING_ENUMERATION_FAILED", why, 3, fetch=fetch.payload())
    scanner = PredecessorScanner(rs, target_sha, ims, args.limit,
                                 collect_graph=bool(args.html))

    queries: list[dict] = []
    resolved: list[str] = []  # 공유 그래프의 시작 커밋들
    for raw in inputs:
        q: dict = {"input": raw, "ftl_sha": None, "ftl_short": None,
                   "status": None, "pegging": None, "self": None,
                   "predecessors": None, "predecessors_total": None,
                   "predecessors_truncated": None, "applied_total": None,
                   "merges_skipped": None, "notes": []}
        queries.append(q)
        full = resolve_commit(ftl, raw, fetch)
        if full is None:
            q["status"] = "not_found_in_ftl"
            q["notes"].append("FTL repo에서 해석 불가 — 전체 sha 또는 --fetch로 재시도")
            continue
        q.update({"ftl_sha": full, "ftl_short": full[:7]})
        resolved.append(full)
        idx, _, _ = rs.locate(full)
        if idx is None:
            q["status"] = "not_pegged"
            q["notes"].append("source branch에 아직 미배달 — 선행 판정은 "
                              "FTL ancestry 기준으로 계속 (배달 전 사전 점검)")
        else:
            q["status"] = "found"
            q["pegging"] = rs.peggings[idx][:7]
        scanner.scan(q, full, idx)

    if args.html:
        # 회사 AI 정책 — 리포트에도 stdout JSON과 같은 정보만 싣는다
        # (sha·날짜·제목·IMS key). 로컬 경로는 stdout JSON에 싣지 않는다.
        why = write_report(args.html, {
            "branch": args.branch,
            "branch_tip": {"sha": tip, "short": tip[:7]},
            "submodule": args.submodule,
            "target": {"ref": args.target, "sha": target_sha,
                       "short": target_sha[:7]},
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "max_graph_nodes": MAX_GRAPH_NODES,
            "queries": queries,
            "graph": scanner.build_graph(resolved),
        })
        if why:
            return fail("REPORT_WRITE_FAILED", why, 3, fetch=fetch.payload())
        rs.note("HTML 리포트 생성됨 (--html)")

    return emit({
        "ok": True,
        "mode": "predecessors",
        "branch": args.branch,
        "branch_tip": {"sha": tip, "short": tip[:7]},
        "submodule": args.submodule,
        "target": {"ref": args.target, "sha": target_sha,
                   "short": target_sha[:7]},
        "queries": queries,
        "fetch": fetch.payload(),
        "notes": rs.notes,
    })


def main() -> int:
    rp = JsonArgumentParser(
        description="사용자에게 확인한 source branch의 FTL sha에 대해, 흐름상 "
                    "먼저 횡전개됐어야 하는데 아직 target branch에 반영되지 "
                    "않은 선행 커밋을 찾는다 (patch 등가 + IMS key 대조).",
        epilog="source branch(--branch)와 FTL target branch(--target)가 "
               "생략되거나 모호하면 실행 전에 사용자에게 먼저 확인할 것 "
               "(추측 금지). 예: predecessors.py --repo ~/integration "
               "--branch origin/develop_XXX --submodule Src/FTL "
               "--ftl-repo ~/FTL --target origin/develop a3f9c21")
    rp.add_argument("shas", nargs="*", metavar="FTL_SHA",
                    help="횡전개 대상 FTL 커밋 sha (여러 개 가능)")
    rp.add_argument("--repo", required=True, help="integration repo clone 경로")
    rp.add_argument("--branch", required=True,
                    help="사용자에게 확인한 source integration 브랜치 "
                         "(예: origin/develop_XXX; 추측 금지)")
    rp.add_argument("--target", required=True,
                    help="사용자에게 확인한 FTL target branch — 횡전개 반영 "
                         "여부 판정의 기준점 (예: origin/develop; FTL repo의 "
                         "remote-tracking ref 권장, 추측 금지)")
    rp.add_argument("--submodule", default="FTL",
                    help="integration tree 안의 FTL gitlink 경로 "
                         "(예: Src/FTL, 기본: FTL)")
    rp.add_argument("--ftl-repo",
                    help="FTL 로컬 clone 경로 (기본: <repo>/<submodule> — "
                         "초기화된 submodule)")
    rp.add_argument("--ims-pattern", default=DEFAULT_IMS_PATTERN,
                    help="커밋 메시지에서 IMS key를 추출하는 정규식 "
                         "(기본: AGCD-134 형태의 대문자 key)")
    rp.add_argument("--html", metavar="PATH",
                    help="self-contained HTML 리포트 출력 경로 — 커밋 클릭으로 "
                         "조상들의 반영 여부를 드릴다운 (stdout JSON은 불변)")
    rp.add_argument("--input", help="sha 목록 파일 (CSV/텍스트 — 각 줄 첫 필드)")
    rp.add_argument("--fetch", action="store_true",
                    help="판정 전에 integration·FTL의 origin을 모두 갱신 "
                         "(하나라도 실패하면 stale 판정을 막기 위해 중단)")
    rp.add_argument("--limit", type=int, default=100,
                    help="선행 커밋 목록 상한 (0=무제한, 기본 100). 초과 시 "
                         "predecessors_truncated=true, *_total은 전체 수")
    rp.add_argument("--thorough", action="store_true",
                    help="pegging 버킷팅에 이진 탐색 대신 전수 선형 스캔 "
                         "(비전진 이력에서 최초 배달 경계를 보장)")
    return cmd_predecessors(rp.parse_args())


if __name__ == "__main__":
    sys.exit(main())
