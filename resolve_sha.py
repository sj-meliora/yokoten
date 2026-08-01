#!/usr/bin/env python3
"""resolve_sha.py — FTL sha에서 배달 pegging과 동반 HAL/Shared/FIL 변경점 해석.

yokoten(횡전개(橫展開) 지원 도구 모음)의 스크립트. 사용자가 확인한 사업화
branch(예: develop_XXX)에서 개발된 변경점을 개발 branch(develop)로 주기적으로
cherry-pick(횡전개)할 때, FTL 커밋 sha만 주어진 상태에서 "같이 반영되어야
하는 HAL/Shared/FIL 커밋"을 찾아준다.

동작 원리:

1. **pegging 역추적** — integration repo의 source branch에서 FTL submodule
   gitlink를 건드린 pegging 커밋들을 열거하고, 주어진 FTL sha F가 "처음
   gitlink 도달 범위에 들어온" 경계 pegging P를 찾는다. FTL은 batch로
   pegging되는 경우가 많아 gitlink 값이 정확히 F인 pegging은 없을 수 있으므로,
   판정은 integration 이력이 아니라 FTL repo의 ancestor 연산
   (`merge-base --is-ancestor F gitlink(P)`)으로 한다.
   - fast path: F가 단독 pegging돼 gitlink 값이 정확히 F였던 경우
     pickaxe(`log -S<F>`)로 즉시 찾는다
   - 일반: gitlink가 전진만 한다는 가정 하에 이진 탐색. 경계 검증에 실패하면
     (reset 등 비전진 이력) 선형 스캔으로 fallback. `--thorough`로 강제 가능
2. **동반 변경 판정** — SOP상 FTL과 엮인 HAL/Shared/FIL 변경은 반드시 같은
   pegging 커밋에 함께 반영되므로, P와 그 first-parent의 diff에서 움직인
   다른 submodule gitlink가 동반 변경의 전부다(같은 pegging에 이동이 없으면
   "동반 없음"이 확정이다). 각 sub repo에서 gitlink 전후의 rev-list 차집합으로
   실제 동반 커밋 목록을 얻는다.

   - 이동 없음                      → no_companion       (확정 — 단독 pick 가능)
   - 이동 있음 + FTL batch 1개      → coupled            (확정 — 세트로 pick)
   - 이동 있음 + FTL batch 여러 개  → coupled_ambiguous  (batch 중 어느 FTL
     커밋과 엮였는지는 사람이 판단 — 후보만 제시)
   - pegging에 부모 없음            → unknown            (판정 불가)

회사 AI 정책에 따라 출력에 author 등 개발자 식별 정보는 싣지 않는다
(sha·날짜·제목만). stdout JSON에는 remote URL·repo 경로·git stderr를 싣지
않는다.

exit code: 0=성공 (개별 sha의 not_pegged 등은 queries[].status로 보고) /
2=인자·검증 오류 / 3=repo 접근 오류
"""

import argparse
from dataclasses import dataclass
import json
import re
import subprocess
import sys
from pathlib import Path

SCHEMA_VERSION = 1
HEX_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
NULL_SHA_RE = re.compile(r"^0+$")


def emit(payload: dict, code: int = 0) -> int:
    json.dump({"schema_version": SCHEMA_VERSION, **payload},
              sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return code


def fail(error_code: str, msg: str, code: int = 2, **extra: object) -> int:
    return emit({"ok": False, "error_code": error_code, "error": msg, **extra}, code)


class JsonArgumentParser(argparse.ArgumentParser):
    """argparse 오류도 계약(JSON stdout + error_code + exit 2)을 따르게 한다."""

    def error(self, message: str) -> None:  # type: ignore[override]
        fail("INVALID_ARGUMENT", message)
        raise SystemExit(2)


@dataclass
class FetchReport:
    """`--fetch`의 실제 수행 여부와 결과를 stdout용으로 누적한다."""

    requested: bool
    attempted: bool = False
    status: str = "not_requested"
    attempts: int = 0
    successes: int = 0
    repositories: dict[str, str] | None = None

    def __post_init__(self) -> None:
        self.repositories = {}
        if self.requested:
            self.status = "skipped"

    def found_locally(self) -> None:
        if self.requested and not self.attempted:
            self.status = "not_needed"

    def record(self, succeeded: bool) -> None:
        self.attempted = True
        self.attempts += 1
        self.successes += int(succeeded)
        if self.successes == self.attempts:
            self.status = "succeeded"
        elif self.successes == 0:
            self.status = "failed"
        else:
            self.status = "partially_succeeded"

    def payload(self) -> dict:
        return {"requested": self.requested, "attempted": self.attempted,
                "status": self.status,
                "repositories": self.repositories}

    def refresh(self, repo: Path, label: str) -> bool:
        """remote-tracking refs를 판정 전에 갱신한다.

        object가 이미 로컬에 있다는 이유로 fetch를 생략하면 FTL만 최신이고
        integration branch는 오래된 비대칭 snapshot으로 판정할 수 있다.
        """
        if not self.requested:
            return True
        rc, _, _ = git(repo, "fetch", "--quiet", "origin")
        ok = rc == 0
        self.record(ok)
        assert self.repositories is not None
        self.repositories[label] = "succeeded" if ok else "failed"
        return ok


def git(repo: Path, *args: str) -> tuple[int, str, str]:
    p = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def is_git_repo(repo: Path) -> bool:
    return git(repo, "rev-parse", "--git-dir")[0] == 0


def resolve_commit(repo: Path, rev: str, fetch: FetchReport) -> str | None:
    """rev → 전체 commit sha. unreachable이어도 object만 있으면 된다."""
    rc, out, _ = git(repo, "rev-parse", "--verify", "--quiet", rev + "^{commit}")
    if rc == 0:
        fetch.found_locally()
        return out
    if fetch.requested:
        fetch_rc, _, _ = git(repo, "fetch", "--quiet", "origin", rev)
        rc, out, _ = git(repo, "rev-parse", "--verify", "--quiet", rev + "^{commit}")
        succeeded = fetch_rc == 0 and rc == 0
        fetch.record(succeeded)
        if succeeded:
            return out
    return None


def gitlink_at(repo: Path, commit: str, subpath: str) -> str | None:
    """commit 트리의 submodule gitlink sha (gitlink가 아니면 None)."""
    rc, out, _ = git(repo, "ls-tree", commit, "--", subpath)
    if rc != 0 or not out:
        return None
    mode, otype, sha = out.split(None, 3)[:3]
    if mode != "160000" or otype != "commit":
        return None
    return sha


def is_ancestor(repo: Path, a: str, b: str) -> bool | None:
    rc, _, _ = git(repo, "merge-base", "--is-ancestor", a, b)
    return rc == 0 if rc in (0, 1) else None  # object 부재 등 판정 불가 → None


def commit_meta(repo: Path, sha: str) -> dict:
    rc, out, _ = git(repo, "show", "-s", "--format=%H%x1f%cs%x1f%s", sha)
    if rc != 0:
        return {"sha": sha, "short": sha[:7], "date": None, "subject": None}
    full, date, subject = out.split("\x1f", 2)
    return {"sha": full, "short": full[:7], "date": date, "subject": subject}


def list_commits(repo: Path, spec: str, limit: int) -> tuple[list[dict], int, bool]:
    """spec('A..B') 차집합의 커밋 목록 (오래된 순 = pick 순서). (목록, 전체, 절단)."""
    rc, out, err = git(repo, "log", "--format=%H%x1f%cs%x1f%s", spec)
    if rc != 0:
        # git stderr에는 remote URL 등 비공개 정보가 섞일 수 있어 전달하지 않는다
        raise RuntimeError("git log failed")
    commits = []
    for line in out.splitlines():
        sha, date, subject = line.split("\x1f", 2)
        commits.append({"sha": sha, "short": sha[:7], "date": date,
                        "subject": subject})
    commits.reverse()  # 오래된 순 — cherry-pick 적용 순서와 일치
    total = len(commits)
    if limit and total > limit:
        return commits[:limit], total, True
    return commits, total, False


# ------------------------------------------------------------------ resolver

class Resolver:
    """source branch의 pegging 목록 위에서 FTL sha들을 경계 탐색으로 해석한다."""

    def __init__(self, integ: Path, ftl: Path, branch: str, subpath: str,
                 fetch: FetchReport, limit: int, thorough: bool,
                 sub_repos: dict[str, Path]):
        self.integ = integ
        self.ftl = ftl
        self.branch = branch
        self.subpath = subpath
        self.fetch = fetch
        self.limit = limit
        self.thorough = thorough
        self.sub_repos = sub_repos
        self.notes: list[str] = []
        self._gitlink: dict[int, str | None] = {}
        self._pred: dict[tuple[str, int], bool] = {}
        self.peggings: list[str] = []
        self._index: dict[str, int] = {}

    def note(self, msg: str) -> None:
        if msg not in self.notes:
            self.notes.append(msg)

    def load_peggings(self) -> str | None:
        """FTL gitlink를 건드린 first-parent 커밋 열거 (오래된 순). 오류 사유 반환."""
        rc, out, _ = git(self.integ, "log", "--first-parent", "--format=%H",
                         self.branch, "--", self.subpath)
        if rc != 0:
            return f"{self.branch!r}에서 pegging 열거 실패 — 사용자에게 확인한 " \
                   "source 브랜치명인지 확인 (예: origin/develop 또는 " \
                   "origin/develop_XXX)"
        self.peggings = list(reversed(out.splitlines()))
        if not self.peggings:
            return f"{self.branch!r}에 {self.subpath!r} gitlink를 건드린 커밋 없음 — --submodule 확인"
        self._index = {sha: i for i, sha in enumerate(self.peggings)}
        return None

    def gitlink(self, idx: int) -> str | None:
        if idx not in self._gitlink:
            sha = gitlink_at(self.integ, self.peggings[idx], self.subpath)
            if sha is None:
                self.note(f"pegging {self.peggings[idx][:7]}의 {self.subpath!r}가 "
                          "gitlink가 아님 — 판정에서 제외")
            self._gitlink[idx] = sha
        return self._gitlink[idx]

    def pred(self, ftl_sha: str, idx: int) -> bool:
        """F가 idx번째 pegging의 gitlink 도달 범위에 포함되는가."""
        key = (ftl_sha, idx)
        if key in self._pred:
            return self._pred[key]
        val = False
        link = self.gitlink(idx)
        if link is not None:
            if resolve_commit(self.ftl, link, self.fetch) is None:
                self.note(f"FTL repo에 gitlink {link[:7]} object 없음 — 미포함으로 "
                          "간주 (--fetch 재시도 가능)")
            else:
                anc = is_ancestor(self.ftl, ftl_sha, link)
                if anc is None:
                    self.note(f"gitlink {link[:7]} ancestor 판정 불가 — 미포함으로 간주")
                else:
                    val = anc
        self._pred[key] = val
        return val

    def _linear(self, ftl_sha: str) -> int | None:
        """오래된 순 전수 스캔 — 최초 배달 경계. 비전진 이력에서도 안전."""
        for i in range(len(self.peggings)):
            if self.pred(ftl_sha, i):
                return i
        return None

    def _pickaxe(self, ftl_sha: str) -> int | None:
        """gitlink 값이 정확히 F였던 pegging (단독 pegging fast path)."""
        rc, out, _ = git(self.integ, "log", "--first-parent", f"-S{ftl_sha}",
                         "--format=%H", self.branch, "--", self.subpath)
        if rc != 0 or not out:
            return None
        hits = [self._index[s] for s in out.splitlines()
                if s in self._index and self.gitlink(self._index[s]) == ftl_sha]
        return min(hits) if hits else None

    def _binary(self, ftl_sha: str) -> tuple[int | None, str]:
        """전진 가정 이진 탐색. 비전진 감지 시 선형 스캔 fallback."""
        lo, hi = 0, len(self.peggings)
        while lo < hi:
            mid = (lo + hi) // 2
            if self.pred(ftl_sha, mid):
                hi = mid
            else:
                lo = mid + 1
        if lo == len(self.peggings):
            return None, "binary"
        if lo > 0 and self.pred(ftl_sha, lo - 1):
            # 단조성 붕괴(reset 등) — 이진 탐색 결과를 버리고 전수 스캔
            self.note("gitlink 비전진 이력 감지 — 선형 스캔으로 전환")
            return self._linear(ftl_sha), "linear"
        return lo, "binary"

    def locate(self, ftl_sha: str) -> tuple[int | None, str, bool]:
        """F의 배달 pegging index 탐색. 반환 (index|None, 방식, 정확일치 여부)."""
        if self.thorough:
            return self._linear(ftl_sha), "linear", False

        idx = self._pickaxe(ftl_sha)
        if idx is not None:
            if idx > 0 and self.pred(ftl_sha, idx - 1):
                # 정확일치 pegging 이전에 이미 포함 — 재pegging 등 비전진 이력
                return self._linear(ftl_sha), "linear", False
            return idx, "pickaxe", True

        idx, how = self._binary(ftl_sha)
        return idx, how, False

    def locate_ancestor(self, ftl_sha: str) -> int | None:
        """pickaxe 없이 경계 pegging index만 탐색.

        predecessors.py가 후보 커밋을 대량 버킷팅할 때 사용한다 — 후보마다
        integration 전체 pickaxe(log -S)를 도는 비용을 피하고, pred 캐시를
        공유하는 이진 탐색(비전진 시 선형 fallback)만 수행한다.
        """
        if self.thorough:
            return self._linear(ftl_sha)
        return self._binary(ftl_sha)[0]

    # ---------------------------------------------------------- 결과 조립

    def companion_repo(self, path: str) -> Path | None:
        if path in self.sub_repos:
            repo = self.sub_repos[path]
            return repo if is_git_repo(repo) else None
        cand = self.integ / path
        if not is_git_repo(cand):
            return None
        # 미초기화 submodule은 빈 디렉토리라 상위(integration) repo로 해석된다
        if git(cand, "rev-parse", "--show-toplevel")[1] == \
                git(self.integ, "rev-parse", "--show-toplevel")[1]:
            return None
        return cand

    def companions_of(self, pegging_sha: str) -> tuple[list[dict] | None, list[str]]:
        """P vs first-parent의 gitlink 이동 (FTL 제외). (목록|판정불가, notes)."""
        notes: list[str] = []
        rc, parent, _ = git(self.integ, "rev-parse", "--verify", "--quiet",
                            pegging_sha + "^")
        if rc != 0:
            return None, ["pegging에 부모 커밋 없음 — 동반 변경 판정 불가"]
        rc, out, _ = git(self.integ, "diff-tree", "--no-renames", "-r", "--raw",
                         parent, pegging_sha)
        if rc != 0:
            return None, ["pegging diff 읽기 실패 — 동반 변경 판정 불가"]
        result = []
        for line in out.splitlines():
            if not line.startswith(":"):
                continue
            front, _, path = line.partition("\t")
            old_mode, new_mode, old_sha, new_sha = front[1:].split()[:4]
            if "160000" not in (old_mode, new_mode) or path == self.subpath:
                continue
            entry: dict = {"path": path,
                           "from": None if NULL_SHA_RE.match(old_sha) else old_sha,
                           "to": None if NULL_SHA_RE.match(new_sha) else new_sha,
                           "commits": None, "commits_total": None,
                           "commits_truncated": None, "removed_total": None,
                           "repo_available": False}
            repo = self.companion_repo(path)
            if repo is None:
                notes.append(f"{path!r} repo 접근 불가 — gitlink 전후 sha만 보고 "
                             "(--sub-repo PATH=DIR로 지정)")
            elif entry["from"] is None or entry["to"] is None:
                notes.append(f"{path!r} submodule이 이 pegging에서 추가/제거됨 — "
                             "커밋 목록 생략")
            else:
                try:
                    added, total, trunc = list_commits(
                        repo, f"{entry['from']}..{entry['to']}", self.limit)
                    _, removed_total, _ = list_commits(
                        repo, f"{entry['to']}..{entry['from']}", self.limit)
                except RuntimeError:
                    notes.append(f"{path!r} 커밋 열거 실패 — gitlink object가 없으면 "
                                 "--fetch 후 재시도")
                else:
                    entry.update({"commits": added, "commits_total": total,
                                  "commits_truncated": trunc,
                                  "removed_total": removed_total,
                                  "repo_available": True})
                    if removed_total:
                        notes.append(f"{path!r} gitlink 비전진 이동 — removed "
                                     f"{removed_total}건은 되돌려진 커밋")
            result.append(entry)
        return result, notes

    def build_pegging(self, idx: int, queried: set[str]) -> dict:
        pegging_sha = self.peggings[idx]
        notes: list[str] = []
        block: dict = {"pegging": commit_meta(self.integ, pegging_sha),
                       "prev_pegging": None, "ftl": None,
                       "companion_status": None, "companions": [],
                       "notes": notes}

        link_to = self.gitlink(idx)
        batch_total = None
        if idx > 0:
            prev_sha = self.peggings[idx - 1]
            block["prev_pegging"] = commit_meta(self.integ, prev_sha)
            link_from = self.gitlink(idx - 1)
            ftl_block: dict = {"from": link_from, "to": link_to,
                               "range": f"{(link_from or '?')[:7]}..{(link_to or '?')[:7]}",
                               "batch": None, "batch_total": None,
                               "batch_truncated": None, "removed_total": None}
            if link_from and link_to:
                try:
                    batch, batch_total, trunc = list_commits(
                        self.ftl, f"{link_from}..{link_to}", self.limit)
                    _, removed_total, _ = list_commits(
                        self.ftl, f"{link_to}..{link_from}", self.limit)
                except RuntimeError:
                    notes.append("FTL batch 열거 실패 — gitlink object가 없으면 "
                                 "--fetch 후 재시도")
                else:
                    for c in batch:
                        c["queried"] = c["sha"] in queried
                    ftl_block.update({"batch": batch, "batch_total": batch_total,
                                      "batch_truncated": trunc,
                                      "removed_total": removed_total})
                    if removed_total:
                        notes.append(f"FTL gitlink 비전진 이동 — removed "
                                     f"{removed_total}건은 되돌려진 커밋")
            block["ftl"] = ftl_block
        else:
            block["ftl"] = {"from": None, "to": link_to,
                            "range": f"?..{(link_to or '?')[:7]}",
                            "batch": None, "batch_total": None,
                            "batch_truncated": None, "removed_total": None}
            notes.append("브랜치 이력에서 가장 오래된 pegging — 직전 gitlink를 알 수 "
                         "없어 batch 미계산. 조회 sha가 이력 시작 전에 이미 포함됐을 수 있음")

        companions, comp_notes = self.companions_of(pegging_sha)
        notes.extend(comp_notes)
        if companions is None:
            block["companion_status"] = "unknown"
        elif not companions:
            block["companion_status"] = "no_companion"
        else:
            block["companions"] = companions
            if batch_total == 1:
                block["companion_status"] = "coupled"
            else:
                block["companion_status"] = "coupled_ambiguous"
                if batch_total is None:
                    notes.append("FTL batch 크기를 알 수 없어 coupled 확정 불가")
                else:
                    notes.append(f"FTL batch {batch_total}건 — 동반 변경이 batch 중 "
                                 "어느 커밋과 엮였는지는 변경 파일·제목으로 판단할 것")
        return block


# ------------------------------------------------------------------ CLI

def read_input_file(path: str) -> tuple[list[str], int]:
    """CSV/텍스트에서 sha 토큰 수집 (각 줄 첫 필드). (목록, 건너뛴 줄 수)."""
    tokens, skipped = [], 0
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        first = re.split(r"[,\s;]+", line)[0].strip().strip('"')
        if HEX_RE.match(first):
            tokens.append(first)
        else:
            skipped += 1  # 헤더 줄 등
    return tokens, skipped


def cmd_resolve(args) -> int:
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

    sub_repos: dict[str, Path] = {}
    for spec in args.sub_repo:
        path, sep, repo = spec.partition("=")
        if not sep or not path or not repo:
            return fail("INVALID_ARGUMENT",
                        f"--sub-repo 형식 오류: {spec!r} (PATH=DIR)",
                        fetch=fetch.payload())
        sub_repos[path] = Path(repo).resolve()

    # 어떤 SHA가 이미 존재하는지 확인하기 전에 양쪽 ref를 함께 갱신한다.
    # 특히 origin/* branch가 로컬에 존재하더라도 stale할 수 있으므로
    # resolve_commit()의 on-demand fetch만으로는 충분하지 않다.
    if args.fetch:
        refresh_repos = {"integration": integ, "FTL": ftl}
        refresh_repos.update({f"companion:{path}": repo
                              for path, repo in sub_repos.items()
                              if is_git_repo(repo)})
        failed = [label for label, repo in refresh_repos.items()
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

    rs = Resolver(integ, ftl, args.branch, args.submodule, fetch,
                  args.limit, args.thorough, sub_repos)
    if skipped:
        rs.note(f"--input에서 sha가 아닌 줄 {skipped}건 무시 (헤더 등)")
    why = rs.load_peggings()
    if why:
        return fail("PEGGING_ENUMERATION_FAILED", why, 3, fetch=fetch.payload())

    queries: list[dict] = []
    grouped: dict[int, set[str]] = {}  # pegging index → 조회된 FTL full sha
    for raw in inputs:
        q: dict = {"input": raw, "ftl_sha": None, "ftl_short": None,
                   "status": None, "pegging": None, "search": None,
                   "exact_gitlink_match": None, "notes": []}
        queries.append(q)
        full = resolve_commit(ftl, raw, fetch)
        if full is None:
            q["status"] = "not_found_in_ftl"
            q["notes"].append("FTL repo에서 해석 불가 — 전체 sha 또는 --fetch로 재시도")
            continue
        q.update({"ftl_sha": full, "ftl_short": full[:7]})
        idx, search, exact = rs.locate(full)
        q.update({"search": search, "exact_gitlink_match": exact})
        if idx is None:
            q["status"] = "not_pegged"
            if search == "binary":
                q["notes"].append("현재 tip gitlink 기준 미포함 — 과거 반영 후 "
                                  "되돌려진 경우는 --thorough로 확인")
            continue
        q["status"] = "found"
        q["pegging"] = rs.peggings[idx][:7]
        grouped.setdefault(idx, set()).add(full)

    peggings = [rs.build_pegging(idx, shas)
                for idx, shas in sorted(grouped.items())]
    return emit({
        "ok": True,
        "mode": "resolve",
        "branch": args.branch,
        "branch_tip": {"sha": tip, "short": tip[:7]},
        "submodule": args.submodule,
        "queries": queries,
        "peggings": peggings,
        "fetch": fetch.payload(),
        "notes": rs.notes,
    })


def main() -> int:
    rp = JsonArgumentParser(
        description="사용자에게 확인한 source branch의 FTL sha가 어느 "
                    "integration pegging으로 "
                    "배달됐는지 역추적하고, 같은 pegging에서 함께 움직인 "
                    "HAL/Shared/FIL 등 다른 gitlink에서 동반 cherry-pick "
                    "대상 커밋을 뽑는다.",
        epilog="source branch가 생략되거나 모호하면 실행 전에 사용자에게 "
               "develop인지 정확한 develop_XXX인지 먼저 확인할 것. 예: "
               "resolve_sha.py --repo ~/integration "
               "--branch origin/develop_XXX --submodule Src/FTL "
               "--ftl-repo ~/FTL --sub-repo Src/HAL=~/HAL "
               "--sub-repo Src/Shared=~/Shared --sub-repo Src/FIL=~/FIL "
               "a3f9c21 (각 --sub-repo의 왼쪽은 integration gitlink 경로, "
               "오른쪽은 로컬 clone 경로)")
    rp.add_argument("shas", nargs="*", metavar="FTL_SHA",
                    help="횡전개 대상 FTL 커밋 sha (여러 개 가능)")
    rp.add_argument("--repo", required=True, help="integration repo clone 경로")
    rp.add_argument("--branch", required=True,
                    help="사용자에게 확인한 source integration 브랜치 "
                         "(예: origin/develop 또는 origin/develop_XXX; 추측 금지)")
    rp.add_argument("--submodule", default="FTL",
                    help="integration tree 안의 FTL gitlink 경로 "
                         "(예: Src/FTL, 기본: FTL)")
    rp.add_argument("--ftl-repo",
                    help="FTL 로컬 clone 경로 (기본: <repo>/<submodule> — "
                         "초기화된 submodule)")
    rp.add_argument("--sub-repo", action="append", default=[], metavar="PATH=DIR",
                    help="동반 submodule repo 경로 지정 (예: Src/FIL=~/fil). 미지정 시 "
                         "<repo>/<PATH>의 초기화된 submodule을 시도")
    rp.add_argument("--input", help="sha 목록 파일 (CSV/텍스트 — 각 줄 첫 필드)")
    rp.add_argument("--fetch", action="store_true",
                    help="판정 전에 integration·FTL·지정 companion의 origin을 모두 "
                         "갱신 (하나라도 실패하면 stale 판정을 막기 위해 중단)")
    rp.add_argument("--limit", type=int, default=100,
                    help="커밋 목록 상한 (0=무제한, 기본 100). "
                         "초과 시 *_truncated=true, *_total은 전체 수")
    rp.add_argument("--thorough", action="store_true",
                    help="이진 탐색 대신 전수 선형 스캔 (비전진 이력에서 최초 "
                         "배달 경계를 보장)")
    return cmd_resolve(rp.parse_args())


if __name__ == "__main__":
    sys.exit(main())
