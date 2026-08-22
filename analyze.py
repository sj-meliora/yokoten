#!/usr/bin/env python3
"""analyze.py — FTL 커밋 구간(FROM..TO) 횡전개 일괄 분석 orchestration.

yokoten(횡전개(橫展開) 지원 도구 모음)의 스크립트. "develop_XXX의 FTL 커밋
xxxxx부터 yyyyy까지 분석해달라"는 요청을 한 번에 처리한다:

1. **구간 전개** — FTL repo에서 FROM..TO(양끝 포함)를 커밋 목록으로 펼친다.
   FROM이 TO의 ancestor가 아니면 INVALID_RANGE로 중단한다.
2. **resolve_sha.py 실행** — 구간 커밋들의 배달 pegging·batch·동반
   (HAL/Shared/FIL) 세트를 해석한다.
3. **predecessors.py 실행** — 구간 커밋 각각에 대해 미반영 선행 커밋·기반영
   여부(patch 등가 + IMS key)·blame 기반 risk를 판정한다.
4. **통합 보고서(--html)** — predecessors 보고서에 "pegging·동반 세트 상세"
   (resolve 결과) 섹션을 합친 self-contained HTML 한 장을 쓴다.

두 스크립트는 subprocess로 실행한다 — fetch 규칙(FETCH_FAILED 중단)·판정
로직·출력 정책은 각 스크립트의 계약을 그대로 따르고, 여기서는 조합만 한다.
자식 스크립트가 실패하면 그 error_code와 exit code를 그대로 전달한다.

stdout은 통합 JSON 하나다: {"mode": "analyze", "range": …, "resolve": <resolve
stdout>, "predecessors": <predecessors stdout(graph 제외)>}. 공유 그래프는
크기 때문에 stdout에 싣지 않고 --html 보고서에만 내장한다. --output PATH를
주면 통합 JSON을 파일로 쓰고 stdout에는 두 자식의 요약만 남긴다 — stdout이
잘리는 도구 환경에서 장시간 분석 결과가 유실되는 것을 막는다.

--output-dir DIR을 주면 두 자식이 구간 커밋별 판정 JSON을 증분 생성한다
(<sha>.resolve.json·<sha>.predecessors.json — 판정이 끝난 sha부터 바로).
장시간 실행의 진행 확인·중단 대비용이며, --resume을 붙이면 tip·target·
인수가 일치하는 저장본을 재사용해 끊긴 구간 분석을 이어간다.

회사 AI 정책에 따라 출력에 author 등 개발자 식별 정보는 싣지 않는다
(sha·날짜·제목·IMS key만). stdout JSON에는 remote URL·repo 경로·git stderr를
싣지 않는다. --html 보고서도 같은 범위를 지킨다.

exit code: 0=성공 / 2=인자·검증 오류 / 3=repo 접근·자식 실행 오류
(자식 실패는 자식의 exit code를 그대로 전달)
"""

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from predecessors import MAX_GRAPH_NODES, summarize_predecessors
from predecessors_viz import write_report
from resolve_sha import (HEX_RE, FetchReport, JsonArgumentParser, emit, fail,
                         git, is_ancestor, is_git_repo, resolve_commit,
                         summarize_resolve, write_output)

SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_MAX_RANGE = 100


def run_child(script: str, argv: list[str]) -> tuple[int, dict | None]:
    """자식 스크립트 실행. (exit code, stdout JSON|None)."""
    p = subprocess.run([sys.executable, str(SCRIPT_DIR / script), *argv],
                       capture_output=True, text=True)
    try:
        payload = json.loads(p.stdout)
    except (json.JSONDecodeError, ValueError):
        payload = None
    return p.returncode, payload


def cmd_analyze(args) -> int:
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
    for tok in (args.range_from, args.range_to):
        if not HEX_RE.match(tok):
            return fail("INVALID_ARGUMENT",
                        f"sha 형식 오류: {tok!r} (hex 7~40자리)",
                        fetch=fetch.payload())
    if args.resume and not args.output_dir:
        return fail("INVALID_ARGUMENT", "--resume은 --output-dir와 함께 사용",
                    fetch=fetch.payload())

    # 구간 전개 전에 FTL을 갱신한다 — 자식 스크립트들도 각자 --fetch 규칙을
    # 따르므로 여기 실패는 stale 구간 전개를 막기 위한 조기 중단이다.
    if args.fetch and not fetch.refresh(ftl, "FTL"):
        return fail("FETCH_FAILED",
                    "최신 상태 확인 실패 — stale snapshot 판정을 막기 위해 중단: FTL",
                    3, fetch=fetch.payload())

    lo = resolve_commit(ftl, args.range_from, fetch)
    hi = resolve_commit(ftl, args.range_to, fetch)
    if lo is None or hi is None:
        return fail("RANGE_NOT_FOUND",
                    "FROM/TO를 FTL repo에서 해석 불가 — 전체 sha 또는 --fetch로 "
                    "재시도", fetch=fetch.payload())
    if not is_ancestor(ftl, lo, hi):
        return fail("INVALID_RANGE",
                    "FROM이 TO의 ancestor가 아님 — 같은 branch 이력에서 오래된 "
                    "쪽을 FROM으로 지정", fetch=fetch.payload())
    rc, out, _ = git(ftl, "rev-list", f"{lo}..{hi}")
    if rc != 0:
        return fail("RANGE_ENUMERATION_FAILED", "구간 커밋 열거 실패", 3,
                    fetch=fetch.payload())
    commits = list(reversed(out.splitlines()))  # 오래된 순
    if lo != hi:
        commits.insert(0, lo)  # 양끝 포함
    else:
        commits = [lo]
    if len(commits) > args.max_range:
        return fail("RANGE_TOO_LARGE",
                    f"구간 커밋 {len(commits)}건 > --max-range {args.max_range} — "
                    "구간을 좁히거나 --max-range를 올려서 재실행",
                    fetch=fetch.payload())

    common = ["--repo", str(integ), "--source-branch", args.source_branch,
              "--submodule", args.submodule, "--ftl-repo", str(ftl),
              "--limit", str(args.limit)]
    if args.fetch:
        common.append("--fetch")
    if args.thorough:
        common.append("--thorough")
    if args.output_dir:
        # 두 자식이 같은 디렉터리에 종류별 suffix(<sha>.resolve.json /
        # <sha>.predecessors.json)로 질의 파일을 증분 생성한다
        common.extend(("--output-dir", args.output_dir))
    if args.resume:
        common.append("--resume")

    resolve_argv = list(common)
    for spec in args.sub_repo:
        resolve_argv.extend(("--sub-repo", spec))
    code, resolve_out = run_child("resolve_sha.py", [*resolve_argv, *commits])
    if resolve_out is None:
        return fail("CHILD_OUTPUT_INVALID", "resolve_sha.py 출력 해석 불가", 3,
                    fetch=fetch.payload())
    if code != 0:
        return emit({"ok": False, "mode": "analyze", "stage": "resolve",
                     "error_code": resolve_out.get("error_code", "CHILD_FAILED"),
                     "error": resolve_out.get("error"),
                     "resolve": resolve_out, "fetch": fetch.payload()}, code)

    pred_argv = [*common, "--target-branch", args.target_branch,
                 "--ims-pattern", args.ims_pattern]
    if args.since:
        pred_argv.extend(("--since", args.since))
    if args.html:
        pred_argv.append("--emit-graph")
    code, pred_out = run_child("predecessors.py", [*pred_argv, *commits])
    if pred_out is None:
        return fail("CHILD_OUTPUT_INVALID", "predecessors.py 출력 해석 불가", 3,
                    fetch=fetch.payload())
    if code != 0:
        return emit({"ok": False, "mode": "analyze", "stage": "predecessors",
                     "error_code": pred_out.get("error_code", "CHILD_FAILED"),
                     "error": pred_out.get("error"),
                     "predecessors": pred_out, "fetch": fetch.payload()}, code)

    range_block = {"from": lo, "from_short": lo[:7],
                   "to": hi, "to_short": hi[:7],
                   "commits_total": len(commits)}

    if args.html:
        graph = pred_out.pop("graph", None)
        pred_out.pop("max_graph_nodes", None)
        why = write_report(args.html, {
            "branch": pred_out["branch"],
            "branch_tip": pred_out["branch_tip"],
            "submodule": pred_out["submodule"],
            "target": pred_out["target"],
            "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "max_graph_nodes": MAX_GRAPH_NODES,
            "window": pred_out.get("window"),
            "range": range_block,
            "queries": pred_out["queries"],
            "graph": graph,
            "resolve": {"peggings": resolve_out.get("peggings", []),
                        "notes": resolve_out.get("notes", [])},
        })
        if why:
            return fail("REPORT_WRITE_FAILED", why, 3, fetch=fetch.payload())

    payload = {
        "ok": True,
        "mode": "analyze",
        "branch": args.source_branch,
        "submodule": args.submodule,
        "target": pred_out.get("target"),
        "range": range_block,
        "resolve": resolve_out,
        "predecessors": pred_out,
        "fetch": fetch.payload(),
        "notes": [],
    }
    if args.output:
        why = write_output(args.output, payload)
        if why:
            return fail("OUTPUT_WRITE_FAILED", why, 3, fetch=fetch.payload())
        return emit({
            "ok": True, "mode": "analyze", "output_written": True,
            "branch": args.source_branch, "submodule": args.submodule,
            "target": pred_out.get("target"), "range": range_block,
            "resolve": summarize_resolve(resolve_out),
            "predecessors": summarize_predecessors(pred_out),
            "fetch": fetch.payload(), "notes": [],
        })
    return emit(payload)


def main() -> int:
    rp = JsonArgumentParser(
        description="사용자에게 확인한 source branch의 FTL 커밋 구간(FROM..TO, "
                    "양끝 포함)을 일괄 분석한다 — resolve_sha.py(배달 pegging·"
                    "동반 세트)와 predecessors.py(미반영 선행·기반영 여부)를 "
                    "함께 실행하고 통합 HTML 보고서를 생성.",
        epilog="source branch(--source-branch)와 FTL target branch(--target-branch)가 "
               "생략되거나 모호하면 실행 전에 사용자에게 먼저 확인할 것 "
               "(추측 금지). 예: analyze.py --repo ~/integration "
               "--source-branch origin/develop_XXX --submodule Src/FTL "
               "--ftl-repo ~/FTL --target-branch origin/develop "
               "--sub-repo Src/HAL=~/HAL --html report.html a3f9c21 77d0e4f")
    rp.add_argument("range_from", metavar="FROM",
                    help="구간 시작 FTL 커밋 sha (포함, 오래된 쪽)")
    rp.add_argument("range_to", metavar="TO",
                    help="구간 끝 FTL 커밋 sha (포함, 최신 쪽)")
    rp.add_argument("--repo", required=True, help="integration repo clone 경로")
    rp.add_argument("--source-branch", required=True,
                    help="사용자에게 확인한 source integration 브랜치 "
                         "(예: origin/develop_XXX; 추측 금지)")
    rp.add_argument("--target-branch", required=True,
                    help="사용자에게 확인한 FTL target branch "
                         "(예: origin/develop; 추측 금지)")
    rp.add_argument("--submodule", default="FTL",
                    help="integration tree 안의 FTL gitlink 경로 "
                         "(예: Src/FTL, 기본: FTL)")
    rp.add_argument("--ftl-repo",
                    help="FTL 로컬 clone 경로 (기본: <repo>/<submodule>)")
    rp.add_argument("--sub-repo", action="append", default=[], metavar="PATH=DIR",
                    help="동반 submodule repo 경로 — resolve_sha.py로 전달 "
                         "(예: Src/FIL=~/fil)")
    rp.add_argument("--ims-pattern", default=None,
                    help="IMS key 정규식 — predecessors.py로 전달")
    rp.add_argument("--since", metavar="DATE",
                    help="판정 창 하한 — predecessors.py로 전달 "
                         "(예: '1.year'; 창 밖 조상은 미판정으로 보고)")
    rp.add_argument("--html", metavar="PATH",
                    help="통합 HTML 보고서 출력 경로 (predecessors 보고서 + "
                         "pegging·동반 세트 상세)")
    rp.add_argument("--output", metavar="PATH",
                    help="통합 결과 JSON을 이 파일에 쓰고 stdout에는 요약만 "
                         "남긴다 — stdout이 잘리는 도구 환경에서 결과 유실 방지 "
                         "(stdout에 파일 경로는 싣지 않는다)")
    rp.add_argument("--output-dir", metavar="DIR",
                    help="구간 커밋(sha)별 판정 JSON을 이 디렉터리에 증분 "
                         "생성 — 두 자식 스크립트가 판정이 끝난 sha부터 "
                         "<sha>.resolve.json·<sha>.predecessors.json을 바로 "
                         "쓴다 (장시간 실행의 진행 확인·중단 대비)")
    rp.add_argument("--resume", action="store_true",
                    help="--output-dir 저장본 중 branch tip·target·인수가 "
                         "일치하는 sha는 재판정 없이 재사용 — 끊긴 구간 "
                         "분석 이어가기 (tip이 움직였으면 자동 재판정)")
    rp.add_argument("--fetch", action="store_true",
                    help="판정 전에 관련 repo의 origin을 갱신 (실패 시 중단)")
    rp.add_argument("--limit", type=int, default=100,
                    help="커밋 목록 상한 — 두 스크립트로 전달 (기본 100, "
                         "초과 시 최근 N건만)")
    rp.add_argument("--max-range", type=int, default=DEFAULT_MAX_RANGE,
                    help=f"구간 커밋 수 상한 (기본 {DEFAULT_MAX_RANGE}) — "
                         "초과 시 RANGE_TOO_LARGE로 중단")
    rp.add_argument("--thorough", action="store_true",
                    help="이진 탐색 대신 전수 선형 스캔 — 두 스크립트로 전달")
    args = rp.parse_args()
    if args.ims_pattern is None:
        from predecessors import DEFAULT_IMS_PATTERN
        args.ims_pattern = DEFAULT_IMS_PATTERN
    return cmd_analyze(args)


if __name__ == "__main__":
    sys.exit(main())
