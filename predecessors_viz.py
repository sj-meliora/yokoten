"""predecessors_viz.py — predecessors.py HTML 리포트 렌더링 모듈.

predecessors.py `--html PATH`가 사용하는 시각화 부분만 분리한 모듈이다.
독립 실행 스크립트가 아니라 predecessors.py가 import하는 순수 렌더링
계층으로, git·분석 로직 없이 "payload dict → self-contained HTML 문자열"
변환과 파일 쓰기만 담당한다. predecessors.py와 같은 폴더에 함께 배포한다.

리포트 구성(브라우저 JS가 내장 JSON을 렌더):

- 요약 타일(triage) + 상태 필터·텍스트 검색
- 미반영 커밋 통합 뷰 — 오래된 순, blocking count(막는 질의 수)
- 질의별 상세 — 배달 순서 정렬 + 같은 pegging 그룹핑, 접이식
- 커밋 클릭 → 조상 반영 여부 드릴다운 패널 (공유 그래프를 걷는다)

회사 AI 정책: 리포트에 실리는 정보는 stdout JSON과 같다(sha·날짜·제목·
IMS key). 외부 리소스(CDN·폰트·이미지)는 사용하지 않는다.
"""

import json
from pathlib import Path


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


