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
   제외하고 건수(`merges_skipped`)만 보고한다 — 횡전개는 fast-forward/rebase
   전용이라 정상 이력에는 merge가 없어야 하며, 리포트는 발견 시에만 경고로
   표시한다.
2. **IMS key 2차 판정** — 충돌 해소·squash로 패치가 변형된 pick은 patch-id가
   어긋나 거짓 미반영이 된다. 커밋 메시지의 IMS key(예: AGCD-134)는 횡전개 시
   유지되므로, target 쪽 커밋 메시지에서 같은 key가 발견되면
   `applied_evidence: "ims_key"`(변형 반영 가능성 — 사람이 확인)로 표시한다.
   key 하나가 커밋 여러 개에 걸칠 수 있으므로 자동 제외하지는 않는다.
3. **위험도 분류 (blame 기반)** — F가 고친 줄의 직전 상태(F^)를
   `git blame`으로 조사해, F의 변경 부근(±OVERLAP_MARGIN줄)을 **마지막으로
   만든 커밋**을 찾는다. blame은 F^ 좌표에서 수행하므로 중간 커밋의
   삽입·삭제로 줄 번호가 밀려도 판정이 어긋나지 않는다.
   - `required_first`  — 미반영 선행이 blame에 지목됨: F의 변경이 문자
     그대로 그 커밋의 줄 위에 쌓여 있다 (직접 의존)
   - `same_file`       — 같은 파일을 건드렸지만 F의 변경 부근은 아님 (참고)
   - `independent`     — 건드린 파일 자체가 다름
   한계: blame은 각 줄의 마지막 수정 커밋만 지목한다 — 같은 줄을 거쳐 간
   더 오래된 커밋은 지목된 커밋 쪽 blame으로 연쇄되므로, 오래된 순으로
   pick하면 안전하다. F가 새로 추가한 파일은 old가 없어 blame 대상이 없다.
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

import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from predecessors_viz import write_report
from resolve_sha import (HEX_RE, FetchReport, JsonArgumentParser, Resolver,
                         commit_meta, emit, fail, git, is_ancestor,
                         is_git_repo, read_input_file, resolve_commit)

DEFAULT_IMS_PATTERN = r"\b[A-Z][A-Z0-9]+-\d+\b"
MAX_GRAPH_NODES = 2000  # HTML 리포트에 내장하는 구간 그래프 노드 상한
OVERLAP_MARGIN = 3      # F 변경 부근으로 blame할 줄 여유 (diff context 관행)
FULL_LEN = 1 << 30      # binary 등 범위를 알 수 없는 변경 — 전체로 간주
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
BLAME_HEAD_RE = re.compile(r"^([0-9a-f]{40}) \d+ \d+")


def rev_list(repo: Path, *args: str) -> list[str] | None:
    rc, out, _ = git(repo, "rev-list", *args)
    return out.splitlines() if rc == 0 else None


def diff_hunks(repo: Path, sha: str) \
        -> dict[str, list[tuple[int, int, int, int]]] | None:
    """커밋의 파일별 hunk 좌표 (old_start, old_len, new_start, new_len).

    old 좌표는 부모(sha^) 시점 좌표라 blame 대상 범위로 그대로 쓸 수 있다.
    binary 등 hunk가 없는 변경은 파일 전체 범위로 간주한다.
    """
    rc, out, _ = git(repo, "diff-tree", "--no-commit-id", "--no-renames",
                     "--unified=0", "-r", "--root", "-p", sha)
    if rc != 0:
        return None
    hunks: dict[str, list[tuple[int, int, int, int]]] = {}
    path: str | None = None
    old_path: str | None = None
    for line in out.splitlines():
        if line.startswith("--- "):
            src = line[4:]
            old_path = src[2:] if src.startswith("a/") else None
        elif line.startswith("+++ "):
            dst = line[4:]
            if dst.startswith("b/"):
                path = dst[2:]
            else:  # /dev/null — 파일 삭제는 old 경로 기준
                path = old_path
        elif line.startswith("Binary files ") or \
                line.startswith("GIT binary patch"):
            if path:
                hunks.setdefault(path, []).append((1, FULL_LEN, 1, FULL_LEN))
        elif line.startswith("@@") and path:
            m = HUNK_RE.match(line)
            if not m:
                continue
            hunks.setdefault(path, []).append(
                (int(m[1]), int(m[2]) if m[2] is not None else 1,
                 int(m[3]), int(m[4]) if m[4] is not None else 1))
    return hunks


def changed_files(repo: Path, sha: str) -> set[str] | None:
    rc, out, _ = git(repo, "diff-tree", "--no-commit-id", "--no-renames",
                     "--name-only", "-r", "--root", sha)
    if rc != 0:
        return None
    return {line for line in out.splitlines() if line}


def merge_regions(regions: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[list[int]] = []
    for s, e in sorted(regions):
        if merged and s <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


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

    def blame_regions(self, full: str,
                      hunks: dict[str, list[tuple[int, int, int, int]]]) \
            -> dict[str, set[str]] | None:
        """F 변경 부근(±OVERLAP_MARGIN줄)을 F^에서 blame한 결과.

        반환: path → 그 부근 줄을 마지막으로 만진 커밋 sha 집합. blame은
        F^ 좌표에서 수행하므로 사이 커밋의 삽입·삭제로 줄 번호가 밀려도
        판정이 어긋나지 않는다. F가 새로 만든 파일은 old가 없어 제외.
        root 커밋이면 빈 dict(의존 없음), blame 실패면 None(판정 불가).
        """
        rc, parent, _ = git(self.rs.ftl, "rev-parse", "--verify", "--quiet",
                            full + "^")
        if rc != 0:
            return {}
        blamed: dict[str, set[str]] = {}
        for path, hs in hunks.items():
            regions = []
            for old_start, old_len, _ns, _nl in hs:
                if old_len == 0:  # 순수 삽입 — 삽입 지점의 양옆 줄
                    start, end = old_start, old_start + 1
                else:
                    start, end = old_start, old_start + old_len - 1
                regions.append((max(1, start - OVERLAP_MARGIN),
                                end + OVERLAP_MARGIN))
            rc, out, _ = git(self.rs.ftl, "show", f"{parent}:{path}")
            if rc != 0:
                continue  # F가 새로 추가한 파일 — blame 대상 없음
            nlines = max(1, len(out.splitlines()))
            regions = merge_regions([(s, min(e, nlines))
                                     for s, e in regions if s <= nlines])
            shas: set[str] = set()
            for start, end in regions:
                rc, bout, _ = git(self.rs.ftl, "blame", "--porcelain",
                                  "-L", f"{start},{end}", parent, "--", path)
                if rc != 0:
                    return None
                shas.update(m[1] for m in map(BLAME_HEAD_RE.match,
                                              bout.splitlines()) if m)
            blamed[path] = shas
        return blamed

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

        f_hunks = None if merge_self else diff_hunks(rs.ftl, full)
        f_files = set(f_hunks) if f_hunks is not None else None
        blamed = self.blame_regions(full, f_hunks) \
            if f_hunks is not None else None
        if f_hunks is not None and blamed is None:
            q["notes"].append("blame 실패 — risk 판정 불가 (object가 없으면 "
                              "--fetch 후 재시도)")
        cand = [c for c in reversed(missing) if c != full]  # 오래된 순
        total = len(cand)
        truncated = bool(self.limit) and total > self.limit
        if truncated:
            cand = cand[:self.limit]

        preds = []
        for c in cand:
            c_idx = rs.locate_ancestor(c)
            c_files = changed_files(rs.ftl, c)
            if f_files is None or blamed is None or c_files is None:
                risk, overlap, same_file = "unknown", None, None
            else:
                overlap = sorted(p for p, shas in blamed.items()
                                 if c in shas)
                same_file = sorted((c_files & f_files) - set(overlap))
                if overlap:
                    risk = "required_first"
                elif same_file:
                    risk = "same_file"
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
                "same_file_paths": same_file,
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
                   "subject": None, "date": None,
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
        meta = commit_meta(ftl, full)
        q.update({"ftl_sha": full, "ftl_short": full[:7],
                  "subject": meta["subject"], "date": meta["date"]})
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
