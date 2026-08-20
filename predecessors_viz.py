"""predecessors_viz.py — predecessors.py HTML 리포트 렌더링 모듈 + 재생성 CLI.

predecessors.py `--html PATH`가 사용하는 시각화 부분만 분리한 모듈이다.
git·분석 로직 없는 순수 렌더링 계층으로, "payload dict → self-contained
HTML 문자열" 변환과 파일 쓰기만 담당한다(하위 프로세스·git 실행·resolve_sha
import 금지 — tests/test_viz.py가 이 경계를 검증). predecessors.py와 같은
폴더에 함께 배포한다.

단독 실행도 가능하다 — 이미 `--output`으로 저장된 결과 JSON에서 보고서만
다시 만든다 (분석 재실행 없음, 수십 분짜리 스캔 결과 재활용):

    python3 predecessors_viz.py <저장된 결과.json> --html report.html

predecessors.py·analyze.py 두 가지 `--output` 전체 JSON을 받는다. 저장본에
공유 그래프가 없으면(분석 실행 시 `--emit-graph`/`--html`을 안 쓴 경우)
질의별 상세 테이블은 온전히 렌더되고, 통합 뷰·조상 드릴다운만 빠진 채
보고서에 그 사실이 경고로 표시된다.

리포트 구성(브라우저 JS가 내장 JSON을 렌더):

- 요약 타일(triage) + 상태 필터·텍스트 검색
- 미반영 커밋 통합 뷰 — 오래된 순, blocking count(막는 질의 수)
- 질의별 상세 — 배달 순서 정렬 + 같은 pegging 그룹핑, 접이식
- 커밋 클릭 → 조상 반영 여부 드릴다운 패널 (공유 그래프를 걷는다)

회사 AI 정책: 리포트에 실리는 정보는 stdout JSON과 같다(sha·날짜·제목·
IMS key). 외부 리소스(CDN·폰트·이미지)는 사용하지 않는다. 재생성 CLI의
stdout JSON에도 파일 경로를 싣지 않는다.
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


# self-contained 리포트 — 외부 리소스(CDN·폰트·이미지) 없이 inline CSS/JS만
# 사용한다. 데이터는 <script type="application/json">에 내장하고, 조상 탐색은
# 브라우저에서 부모 edge를 따라 수행한다 (클릭마다 git 재실행 불필요).
HTML_TEMPLATE = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>yokoten — 횡전개 분석 보고서</title>
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
.qhead .qtitle{overflow-wrap:anywhere}
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
main .group{margin:10px 16px}
main .group>h3{font-size:12px;color:var(--muted);margin:0 0 4px;font-weight:600}
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
  <h1>yokoten — 횡전개 분석 보고서</h1>
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
  patch_applied: ["기반영 (diff 동일)", "b-green"],
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
  patch_applied:     ["기반영 (diff 동일)", "b-green"],
  in_target_history: ["target 이력에 포함", "b-green"],
  unknown:           ["판정 불가", "b-gray"],
};
const RISK = {
  required_first: ["required_first — diff 부근 의존 (blame)", "b-red"],
  // same_file·independent는 구버전 저장 JSON 재생성용 — 현재 판정은
  // diff 부근 의존만 선행으로 싣고 나머지는 건수로만 보고한다
  same_file:      ["same_file — 변경 부근 아님", "b-amber"],
  independent:    ["independent", "b-gray"],
  unknown:        ["risk 판정 불가", "b-gray"],
};
// 연쇄 의존 표기 — required_by에 질의 sha가 없으면 다른 선행의 diff 부근에서
// 지목된 간접 의존이다
function viaBadge(pred, q) {
  if (!pred.required_by || !q.ftl_sha || pred.required_by.includes(q.ftl_sha))
    return null;
  return badge(["간접 — " + pred.required_by.map(s => s.slice(0, 7)).join(", ")
                + " 경유", "b-amber"]);
}

// 공유 그래프 — 모든 질의 구간의 합집합 한 벌 (topo 순, 0 = 최신)
const G = (() => {
  const map = new Map(), order = new Map();
  ((DATA.graph || {}).nodes || []).forEach((n, i) => {
    map.set(n.sha, n); order.set(n.sha, i);
  });
  // present=false — 그래프 없이 저장된 JSON에서 재생성된 보고서:
  // 질의별 상세는 온전하지만 통합 뷰·조상 드릴다운은 불가
  return { map, order, truncated: (DATA.graph || {}).truncated,
           present: DATA.graph != null };
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
function sibLabel(entry) {
  // 같이 배달된 sibling gitlink — sha가 있으면 path@sha7, 없으면 경로만
  const links = entry.companion_links;
  if (links && links.length)
    return links.map(l => l.path + (l.to ? "@" + l.to.slice(0, 7) : "")).join(", ");
  return (entry.companions_moved || []).join(", ");
}

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
  if (!n) {
    panel.append(el("p", "empty", G.present ? "그래프에 없는 커밋"
      : "공유 그래프 미포함 — 조상 드릴다운 불가 (분석 시 --html을 직접 쓰거나 "
        + "--emit-graph --output으로 저장한 JSON에서 재생성)"));
    return;
  }

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
    const via = viaBadge(pred, q);
    if (via) extra.append(" ", via);
    const relPaths = (pred.overlap_paths || []).length
      ? pred.overlap_paths : (pred.same_file_paths || []);
    if (relPaths.length)
      extra.append(" " + relPaths.join(", "));
    if (pred.pegging)
      extra.append(" · pegging " + pred.pegging
                   + (pred.same_batch ? " (같은 batch)" : ""));
    const sib = sibLabel(pred);
    if (sib) extra.append(" · 동반 " + sib);
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
  group(panel, "기반영 (diff 동일)", by("patch_applied"), false);
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
    const okT = !t || (d.dataset.hay || "").includes(t);
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
  sec.dataset.hay = [q.input, q.ftl_sha || "", q.subject || "",
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
  // 횡전개 커밋 자신의 제목 — 접힌 상태에서도 바로 보이게
  const qtitle = q.subject
    || (q.ftl_sha && G.map.has(q.ftl_sha) ? G.map.get(q.ftl_sha).subject : "");
  if (qtitle) head.append(el("span", "qtitle", qtitle));
  head.append(badge([QCLASS[cls][0], "b-" + QCLASS[cls][1].slice(2)]));
  if (q.status === "not_pegged") head.append(badge(["not_pegged", "b-amber"]));
  if (q.pegging) head.append(badge(["pegging " + q.pegging, "b-accent"]));
  const qsib = sibLabel(q);
  if (qsib) head.append(badge(["동반 " + qsib, "b-accent"]));
  if (q.self && q.self.applied === "key_matched")
    head.append(badge(SELF_APPLIED.key_matched));
  const sub = el("div", "qsub");
  if (q.self && q.self.ims_keys.length)
    sub.append("IMS key: " + q.self.ims_keys.join(", ") + " · ");
  if (q.predecessors !== null) {
    sub.append("미반영 선행(diff 부근 의존) " + q.predecessors_total
               + "건 · 기반영 선행 " + q.applied_total + "건 (diff 동일)");
    if (q.unrelated_unapplied_total)
      sub.append(" · 부근 무관 미반영 " + q.unrelated_unapplied_total
                 + "건 (선행 아님)");
  }
  head.append(sub);
  sec.append(head);

  for (const note of q.notes || [])
    sec.append(el("div", "warn", note));
  // ff/rebase 전용 흐름에서 merge는 없어야 정상 — 발견 시에만 경고
  if (q.merges_skipped)
    sec.append(el("div", "warn", "merge 커밋 " + q.merges_skipped
      + "건 발견 — fast-forward/rebase 전용 흐름에 어긋남 (patch 판정에서 제외됨)"));
  // --since 창 절단 — 창 밖 조상은 미판정이므로 "선행 없음 확정"이 아니다
  if (q.window_clipped)
    sec.append(el("div", "warn",
      "--since 창 절단 — 창 밖 조상은 미판정 (창 내 미반영이 없어도 선행 없음 확정 아님)"));

  if (q.predecessors === null) {
    sec.append(el("p", "empty", "선행 커밋 판정 없음"));
  } else if (!q.predecessors.length) {
    let msg = "diff 부근 의존 선행 없음 — 단독 pick 가능";
    if (q.unrelated_unapplied_total)
      msg += " (부근 무관 미반영 " + q.unrelated_unapplied_total
           + "건은 통합 뷰·드릴다운에서 확인)";
    sec.append(el("p", "empty", msg));
  } else {
    if (q.predecessors_truncated)
      sec.append(el("div", "warn", "목록이 --limit에서 절단됨 (전체 "
                    + q.predecessors_total + "건 중 최근 항목만 표시)"
                    + (G.present ? " — 그래프 클릭으로는 전부 탐색 가능" : "")));
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
        const sib = sibLabel(p);
        if (sib) peg.append(" ", badge(["동반 " + sib, "b-accent"]));
      } else peg.append("—");
      r.append(peg);
      const risk = el("td");
      risk.append(badge(RISK[p.risk] || [p.risk, "b-gray"]));
      const via = viaBadge(p, q);
      if (via) risk.append(" ", via);
      const riskPaths = (p.overlap_paths || []).length
        ? p.overlap_paths : (p.same_file_paths || []);
      if (riskPaths.length)
        risk.append(" ", el("span", "d", riskPaths.join(", ")));
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
  if (DATA.range)
    meta.append(el("div", null, "분석 구간 " + DATA.range.from_short + ".."
      + DATA.range.to_short + " (" + DATA.range.commits_total + "건, 양끝 포함)"));
  if (DATA.window && DATA.window.since)
    meta.append(el("div", null, "판정 창 --since " + DATA.window.since
      + " · 창 밖 미판정 조상 " + DATA.window.excluded_total + "건"));
  if (DATA.regenerated)
    meta.append(el("div", null, "저장된 결과 JSON에서 재생성한 보고서"));

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
  if (!G.present) {
    root.append(el("div", "warn",
      "공유 그래프 미포함 — 통합 뷰·조상 드릴다운을 쓸 수 없음 (질의별 상세는 온전함). "
      + "분석 시 --html을 직접 쓰거나 --emit-graph --output으로 저장한 JSON에서 "
      + "재생성하면 포함된다"));
  } else if (!blockers.length) {
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
    if (oa !== ob) return ob - oa;  // order 내림차순 = 오래된 순, 해석 불가(-1)는 마지막
    // 그래프 미포함 재생성 — 커밋 날짜로 오래된 순 근사 (topo 순 대체)
    const da = a.date || "", db = b.date || "";
    return da < db ? -1 : da > db ? 1 : 0;
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

  // analyze.py 통합 보고서 — resolve_sha의 pegging·동반 세트 상세
  if (DATA.resolve && (DATA.resolve.peggings || []).length) {
    root.append(el("h2", "sect", "pegging·동반 세트 상세 (배달 단위)"));
    for (const note of DATA.resolve.notes || [])
      root.append(el("div", "warn", note));
    for (const blk of DATA.resolve.peggings) {
      const d = el("details", "query");
      d.dataset.cls = "peg";
      d.dataset.hay = [blk.pegging.sha, blk.pegging.subject || "",
        ...((blk.ftl && blk.ftl.batch) || []).map(c => c.sha + " " + (c.subject || "")),
        ...(blk.companions || []).flatMap(c => [c.path,
          ...(c.commits || []).map(x => x.sha + " " + (x.subject || ""))]),
      ].join(" ").toLowerCase();
      if ((blk.companions || []).length) d.open = true;
      const head = el("summary", "qhead");
      head.append(el("span", "caret", "▶"));
      head.append(el("span", "sha", blk.pegging.short || ""));
      head.append(el("span", "qtitle", blk.pegging.subject || ""));
      const cs = blk.companion_status;
      head.append(badge([
        cs === "no_companion" ? "동반 없음 (확정)"
          : cs === "coupled" ? "동반 세트 (확정)"
          : cs === "coupled_ambiguous" ? "동반 있음 — batch 대응 확인"
          : "동반 판정 불가",
        cs === "no_companion" ? "b-green"
          : cs === "unknown" ? "b-gray" : "b-amber"]));
      const sub = el("div", "qsub");
      const ftl = blk.ftl || {};
      sub.append("FTL batch " + (ftl.batch_total == null ? "?" : ftl.batch_total)
                 + "건 · " + (ftl.range || "?")
                 + (blk.pegging.date ? " · " + blk.pegging.date : ""));
      head.append(sub);
      d.append(head);
      for (const note of blk.notes || [])
        d.append(el("div", "warn", note));
      if ((ftl.batch || []).length) {
        const tb = el("table"), thead = el("thead"), tr = el("tr");
        for (const h of ["sha", "date", "subject", ""])
          tr.append(el("th", null, h));
        thead.append(tr); tb.append(thead);
        const body = el("tbody");
        for (const c of ftl.batch) {
          const inGraph = G.map.has(c.sha);
          const r = el("tr", inGraph ? "commit" : null);
          r.append(el("td", "sha", c.short || c.sha.slice(0, 7)),
                   el("td", "date", c.date || ""),
                   el("td", null, c.subject || ""));
          const mark = el("td");
          if (c.queried) mark.append(badge(["분석 구간 내", "b-accent"]));
          r.append(mark);
          if (inGraph) r.onclick = () => showCommit(c.sha);
          body.append(r);
        }
        tb.append(body);
        d.append(tb);
      }
      for (const comp of blk.companions || []) {
        const cv = el("div", "group");
        cv.append(el("h3", null, comp.path + ": "
          + (comp.from || "없음").slice(0, 7) + " → "
          + (comp.to || "없음").slice(0, 7)
          + (comp.repo_available
             ? " · 동반 커밋 " + comp.commits_total + "건"
             : " · clone 미접근 — gitlink sha만")));
        for (const c of comp.commits || []) {
          const row = el("div", "anc");
          row.append(el("span", "s", c.short || c.sha.slice(0, 7)),
                     el("span", "d", c.date || ""),
                     el("span", "t", c.subject || ""));
          cv.append(row);
        }
        d.append(cv);
      }
      root.append(d);
    }
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


# ------------------------------------------------- 저장된 JSON에서 재생성

def _base_payload(pred: dict) -> dict:
    """predecessors 전체 결과(dict) → write_report payload 공통부.

    graph는 저장본에 있을 때만 실린다(`--emit-graph --output` 실행) — 없으면
    None으로 내장되어 브라우저가 통합 뷰·드릴다운 자리에 경고를 띄운다.
    """
    return {
        "branch": pred.get("branch"),
        "branch_tip": pred.get("branch_tip"),
        "submodule": pred.get("submodule"),
        "target": pred.get("target"),
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "regenerated": True,
        "window": pred.get("window"),
        "max_graph_nodes": pred.get("max_graph_nodes"),
        "queries": pred["queries"],
        "graph": pred.get("graph"),
    }


def payload_from_saved(data: object) -> tuple[dict | None, str | None]:
    """`--output`으로 저장된 전체 결과 JSON → 리포트 payload.

    predecessors.py(mode "predecessors")와 analyze.py(mode "analyze")의
    `--output` 파일을 받는다. stdout 요약(digest)·실패 JSON은 상세가 없어
    렌더할 수 없다. 반환: (payload, 실패 사유) — 성공 시 사유는 None.
    """
    if not isinstance(data, dict):
        return None, "JSON 최상위가 객체가 아님 — --output으로 저장한 전체 결과 파일인지 확인"
    if data.get("output_written"):
        return None, ("stdout 요약(digest) JSON — 선행 상세가 없어 렌더 불가, "
                      "--output으로 저장된 전체 결과 파일을 지정")
    if data.get("ok") is False:
        return None, "실패 결과 JSON — error_code를 확인하고 분석을 재실행"
    mode = data.get("mode")
    if mode == "analyze":
        pred = data.get("predecessors")
        if not isinstance(pred, dict) or "queries" not in pred:
            return None, "analyze 결과에 predecessors 상세 없음"
        resolve = data.get("resolve") or {}
        payload = _base_payload(pred)
        payload["range"] = data.get("range")
        payload["resolve"] = {"peggings": resolve.get("peggings", []),
                              "notes": resolve.get("notes", [])}
        return payload, None
    if mode == "predecessors":
        if "queries" not in data:
            return None, "predecessors 결과에 queries 없음"
        return _base_payload(data), None
    return None, ("지원하지 않는 mode — predecessors.py/analyze.py의 "
                  "--output JSON만 렌더 가능")


def _emit(payload: dict, code: int = 0) -> int:
    # resolve_sha.emit과 같은 형태 — 모듈 경계상 import하지 않고 로컬 구현
    # (schema_version 1은 resolve_sha.SCHEMA_VERSION과 같은 값을 유지한다)
    json.dump({"schema_version": 1, **payload},
              sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return code


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="이미 --output으로 저장된 결과 JSON에서 HTML 보고서만 "
                    "다시 만든다 — 분석(git 스캔) 재실행 없음. "
                    "predecessors.py·analyze.py의 --output 파일을 받는다.",
        epilog="저장본에 공유 그래프가 없으면(분석 시 --emit-graph/--html 미사용) "
               "질의별 상세는 온전히 렌더되고 통합 뷰·조상 드릴다운만 빠진다. "
               "예: predecessors_viz.py result.json --html report.html")
    ap.add_argument("saved_json", metavar="OUTPUT_JSON",
                    help="predecessors.py/analyze.py --output으로 저장된 전체 결과 JSON")
    ap.add_argument("--html", required=True, metavar="PATH",
                    help="self-contained HTML 보고서 출력 경로")
    args = ap.parse_args(argv)
    try:
        data = json.loads(Path(args.saved_json).read_text(encoding="utf-8"))
    except OSError:
        return _emit({"ok": False, "error_code": "INPUT_FILE_UNREADABLE",
                      "error": "저장된 결과 JSON을 읽을 수 없음"}, 3)
    except json.JSONDecodeError:
        return _emit({"ok": False, "error_code": "INVALID_ARGUMENT",
                      "error": "JSON 파싱 실패 — --output으로 저장한 전체 결과 "
                               "파일인지 확인"}, 2)
    payload, why = payload_from_saved(data)
    if payload is None:
        return _emit({"ok": False, "error_code": "INVALID_ARGUMENT",
                      "error": why}, 2)
    why = write_report(args.html, payload)
    if why:
        return _emit({"ok": False, "error_code": "REPORT_WRITE_FAILED",
                      "error": why}, 3)
    notes = []
    if payload["graph"] is None:
        notes.append("공유 그래프 미포함 — 통합 뷰·조상 드릴다운 없음 "
                     "(분석 시 --html을 직접 쓰거나 --emit-graph --output으로 "
                     "저장하면 포함)")
    return _emit({"ok": True, "mode": "render",
                  "source_mode": data.get("mode"),
                  "queries_total": len(payload["queries"]),
                  "graph_embedded": payload["graph"] is not None,
                  "notes": notes})


if __name__ == "__main__":
    sys.exit(main())


