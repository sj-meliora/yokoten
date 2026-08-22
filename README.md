# yokoten

횡전개(橫展開, [yokoten](https://en.wikipedia.org/wiki/Toyota_Production_System))
지원 도구 모음. 확인된 사업화 branch(예: `develop_XXX`)에서 개발된 변경점을 개발
branch(`develop`)로 주기적으로 cherry-pick하는 업무를 돕는다. 기능 하나가
스크립트 하나다:

| 스크립트 | 역할 |
|---|---|
| `resolve_sha.py` | FTL sha → 배달 pegging 역추적 + **같이 반영되어야 하는 HAL/Shared/FIL 커밋** 해석 |
| `predecessors.py` | FTL sha → **그 커밋의 diff 부근에 의존이 걸리는데(blame 연쇄) target에 미반영인 선행 커밋** 탐지 |
| `analyze.py` | FTL 커밋 **구간(FROM..TO) 일괄 분석** — 위 두 스크립트를 실행하고 통합 보고서 생성 |

`predecessors_viz.py`는 HTML 보고서 렌더링 모듈이다(git·분석 로직 없음) —
배포 시 네 파일을 같은 폴더에 함께 둔다. 단독 실행하면 이미 `--output`으로
저장된 결과 JSON에서 **분석 재실행 없이** 보고서만 다시 만든다(아래
"저장된 JSON에서 보고서 재생성" 참고).

Python 3.10+ 표준 라이브러리와 git CLI만 사용한다 (외부 의존성 없음).

## 배경

- 횡전개 대상은 excel로 관리되며, 거기에는 **FTL 기준 sha**만 적혀 있다.
- FTL 변경은 integration repo에 submodule gitlink 갱신(pegging)으로 반영되고,
  **batch로 pegging되는 경우가 많다** — 특정 FTL sha와 1:1 대응하는 pegging이
  없을 수 있다.
- **SOP: FTL과 엮인 HAL/Shared/FIL 변경은 반드시 같은 pegging 커밋에 함께
  반영된다.** 따라서 배달 pegging에서 다른 gitlink가 움직이지 않았다면
  "동반 변경 없음"이 확정이고, 움직였다면 그것이 동반 후보의 전부다.

## 사용법 — resolve_sha.py

> **Agent 필수 확인 사항:** 사용자가 source integration branch를 명시하지 않았다면
> Git 명령이나 스크립트를 실행하기 전에 먼저 `develop`인지, 정확히 어떤
> `develop_XXX`인지 질문한다. 예시를 근거로 `develop_Evan`을 가정하거나,
> remote branch를 검색해서 그럴듯한 branch를 대신 고르면 안 된다. 사용자가 이미
> branch를 명확히 지정한 경우에만 다시 묻지 않고 해당 remote-tracking ref를 쓴다.

```
resolve_sha.py --repo <integration clone> --source-branch origin/<CONFIRMED_BRANCH> \
               [--submodule PATH] [--ftl-repo DIR] [--sub-repo PATH=DIR ...] \
               [--fetch] [--limit N] [--thorough] [--output PATH] \
               <FTL_SHA> [<FTL_SHA> ...]            # 또는 --input picks.csv
```

실제 `Src/*` 배치에서는 다음처럼 지정한다.

```sh
python3 resolve_sha.py \
  --repo ~/work/integration \
  --source-branch origin/develop_XXX \
  --submodule Src/FTL \
  --ftl-repo ~/work/FTL \
  --sub-repo Src/HAL=~/work/HAL \
  --sub-repo Src/Shared=~/work/Shared \
  --sub-repo Src/FIL=~/work/FIL \
  a3f9c21 77d0e4f
```

각 인자의 의미는 다음과 같다.

| 인자 | 예시 | 의미 |
|---|---|---|
| `--repo` | `~/work/integration` | pegging commit을 조회할 integration clone |
| `--source-branch` | `origin/develop_XXX` | 사용자에게 확인한 pegging 조회 integration branch |
| `--submodule` | `Src/FTL` | integration **tree 안에서의** FTL gitlink 경로 |
| `--ftl-repo` | `~/work/FTL` | ancestor/batch 조회에 사용할 FTL **로컬 clone** |
| `--sub-repo` | `Src/FIL=~/work/FIL` | `gitlink 경로=로컬 clone 경로`; 필요한 만큼 반복 |
| `--fetch` |  | 판정 전에 integration·FTL·지정 companion의 `origin`을 함께 갱신; 하나라도 실패하면 판정 중단 |
| `--output` | `result.json` | 전체 결과 JSON을 파일로 쓰고 stdout에는 요약(집계 + sha별 digest)만 남김 — stdout이 잘리는 도구 환경(agent·CI)에서 결과 유실 방지. 실패 시 `OUTPUT_WRITE_FAILED`(exit 3). 경로 금지 정책에 따라 stdout에 파일 경로는 싣지 않음 |
| `--output-dir` | `probe/` | **질의(commit) 단위 증분 저장** — sha 하나의 판정이 끝날 때마다 `<sha>.resolve.json`(판정 + 그 pegging·동반 상세)을 바로 씀. 장시간 일괄 실행이 끊겨도 완료분은 남고, 진행 상황이 파일 개수로 보임 (아래 "질의(commit) 단위 증분 저장" 참고) |
| `--resume` |  | `--output-dir` 저장본 중 branch tip·인수가 일치하는 sha는 재판정 없이 재사용 — 끊긴 일괄 실행 이어가기 |
| 마지막 인자 | `a3f9c21` | 찾으려는 FTL commit SHA; 여러 개 지정 가능 |

따라서 `Src/FIL=~/work/FIL`에서 `Src/FIL`은 integration checkout 안의
디렉터리 경로이고, `~/work/FIL`은 FIL commit object를 읽을 수 있는 별도 clone
경로다. 두 값이 같은 경로일 필요는 없다.

### `--sub-repo`가 필요한 경우

`--sub-repo`는 동반 변경을 **발견**하는 옵션이 아니다. 어떤 gitlink가 함께
움직였는지는 integration repo의 pegging diff에서 옵션과 무관하게 항상 찾아낸다.
`--sub-repo`는 그렇게 발견된 sibling의 **커밋 목록을 펼칠 때 읽을 로컬 clone
위치**만 알려준다. sibling repo 탐색 우선순위는 다음과 같다.

| 상황 | `--sub-repo` | 결과 |
|---|---|---|
| `<repo>/<PATH>`에 초기화된 submodule 있음 | 불필요 (자동 사용) | 커밋 목록까지 보고 |
| submodule 미초기화(빈 폴더), 별도 clone 있음 | `PATH=DIR` 지정 | 커밋 목록까지 보고 |
| 읽을 수 있는 clone이 아예 없음 | 생략 | gitlink 전후 SHA만 보고 (`commits: null`) — 판정 자체는 정상 |

submodule 폴더는 `git clone`만으로는 채워지지 않는다는 점에 주의
(`git submodule update --init` 필요). sibling을 init하지 않은 integration
checkout에서는, repo를 새로 받는 대신 이미 갖고 있는 standalone clone을
`--sub-repo`로 재활용하면 된다.

- sha 여러 개를 한 번에 넘기면 **pegging 단위로 그룹핑**되어 나온다 — 같은
  batch로 배달된 sha들은 한 세트로 판단할 수 있다.
- `--input`은 excel export(CSV/텍스트)를 그대로 받는다. 각 줄의 첫 필드가
  sha면 수집하고, 헤더 등 나머지 줄은 무시 후 `notes`에 집계한다.
- `--sub-repo`의 필요 여부는 위 표를 따른다 — 초기화된 submodule이 있으면
  생략하고, 빈 폴더면 standalone clone을 연결한다 (예:
  `--sub-repo Src/FIL=~/fil`).

`--fetch`는 단순히 입력 SHA가 없을 때만 FTL을 fetch하지 않는다. **판정을
시작하기 전에 integration remote-tracking branch와 FTL, 명시한 companion
repo를 한 묶음으로 fetch**한다. 이 중 하나라도 갱신하지 못하면 기존 checkout
기준으로 `not_pegged`를 내리지 않고 `FETCH_FAILED`(exit 3)로 중단한다. 따라서
"최신 FTL SHA + 오래된 integration branch"가 섞여 거짓 `not_pegged`가 되는
상황을 막으려면 자동화 호출에 `--fetch`를 사용해야 한다. `--source-branch`에는 fetch로
갱신되는 `origin/develop_XXX` 같은 remote-tracking ref를 권장한다(로컬 branch는
fetch해도 자동 fast-forward되지 않는다).

### 질의(commit) 단위 증분 저장 (`--output-dir`, `--resume`)

sha 여러 개의 장시간 일괄 실행은 끝까지 가야 결과가 나온다 — 도중에
끊기면(타임아웃·중단) 전부 유실된다. `--output-dir DIR`을 주면 일괄 실행
구조(공유 스캔 1회)는 그대로 두고, **질의 하나의 판정이 끝날 때마다**
`<sha>.resolve.json`(`predecessors.py`는 `<sha>.predecessors.json`)을 그
디렉터리에 바로 쓴다.

- 파일은 임시 파일에 쓴 뒤 rename하므로 반쯤 쓰인 파일이 읽히지 않는다 —
  실행 중에도 완료된 sha의 파일을 안전하게 열어볼 수 있다.
- 각 파일에는 실행 context(branch tip·target·판정 인수)와 그 질의의 전체
  판정이 담긴다. `resolve_sha.py` 파일에는 해당 pegging·동반 세트 상세
  (`pegging_detail`)도 함께 실린다.
- FTL repo에서 해석되는 sha는 전체 sha가 파일명이고, 해석 불가 입력은
  입력 토큰이 파일명이다.
- 같은 인수에 `--resume`을 붙여 재실행하면 context가 완전히 일치하는
  저장본은 재판정 없이 재사용한다 — 끊긴 실행을 이어가는 용도. branch
  tip·target이 움직였거나 인수가 다르면 조용히 전부 재판정한다 (stale
  저장본으로 판정하지 않는다는 `--fetch` 규칙과 같은 원칙). 해석
  불가(`not_found_in_ftl`)·판정 미완(스캔 실패, `--since` 창 밖) 저장본도
  재사용하지 않는다.
- stdout에는 경로 없이 집계만 실린다: `commit_files: {written, reused,
  write_failed}`. 파일 쓰기 실패는 실행을 중단하지 않고 건수·notes로
  보고한다 — 전체 결과는 여전히 stdout/`--output`이 계약이다.

주의: `--resume`은 patch 등가 스캔(`predecessors.py`의 지배 비용)까지
건너뛰지는 못한다 — 재사용은 질의별 비용(pegging 탐색·blame 연쇄)만
아낀다. 다만 모든 질의가 재사용되고 보고서(`--html`/`--emit-graph`)도
요청하지 않으면 스캔 자체를 생략한다.

## 판정 로직

1. **pegging 역추적** — source branch에서 FTL gitlink를 건드린 first-parent
   커밋(pegging)을 열거하고, 주어진 FTL sha `F`가 "처음 gitlink 도달 범위에
   들어온" 경계 pegging을 찾는다. 포함 판정은 integration 이력이 아니라
   **FTL repo의 `merge-base --is-ancestor`**로 한다 (batch pegging 대응).
   - fast path: `F`가 단독 pegging돼 gitlink 값과 정확히 일치하면
     pickaxe(`log -S`)로 즉시 찾는다 (`exact_gitlink_match: true`)
   - 일반: gitlink 전진이 단조라는 가정 하에 이진 탐색. 경계 검증 실패
     (reset 등 비전진 이력) 시 자동으로 선형 스캔 fallback. `--thorough`는
     처음부터 전수 스캔해 비전진 이력에서도 최초 배달 경계를 보장한다
2. **동반 변경 판정** — 경계 pegging `P`와 first-parent의 diff에서 움직인
   다른 submodule gitlink를 수집하고, 각 sub repo에서 gitlink 전후 rev-list
   차집합으로 실제 커밋 목록을 얻는다.

| `companion_status` | 의미 | 후속 조치 |
|---|---|---|
| `no_companion` | 같은 pegging에 다른 gitlink 이동 없음 (SOP상 확정) | FTL만 단독 pick |
| `coupled` | 동반 이동 + FTL batch 1건 | FTL·동반 커밋을 한 세트로 pick |
| `coupled_ambiguous` | 동반 이동 + FTL batch 여러 건 | 어느 FTL 커밋과 엮였는지 변경 파일·제목으로 판단 |
| `unknown` | pegging에 부모 없음 등 판정 불가 | 수동 확인 |

`queries[].status`는 sha별 해석 결과다: `found` / `not_pegged`(어느 pegging
gitlink에도 미포함 — excel 오류이거나 아직 미반영) / `not_found_in_ftl`.

## 출력 (JSON, stdout)

```json
{
  "schema_version": 1, "ok": true, "mode": "resolve",
  "branch": "origin/develop_XXX", "branch_tip": {"sha": "…", "short": "…"},
  "queries": [
    {"input": "a3f9c21", "ftl_sha": "…", "status": "found",
     "pegging": "77d0e4f", "search": "binary", "exact_gitlink_match": false,
     "notes": []}
  ],
  "peggings": [
    {"pegging": {"sha": "…", "date": "…", "subject": "…"},
     "prev_pegging": {"sha": "…"},
     "ftl": {"from": "…", "to": "…", "range": "…",
             "batch": [{"sha": "…", "date": "…", "subject": "…", "queried": true}],
             "batch_total": 2, "removed_total": 0},
     "companion_status": "coupled_ambiguous",
     "companions": [
       {"path": "HAL", "from": "…", "to": "…",
        "commits": [{"sha": "…", "date": "…", "subject": "…"}],
        "commits_total": 1, "removed_total": 0, "repo_available": true}
     ],
     "notes": ["…"]}
  ],
  "fetch": {"requested": false, "attempted": false,
            "status": "not_requested", "repositories": {}},
  "notes": []
}
```

- `batch`·`commits`는 **오래된 순** — cherry-pick 적용 순서와 같다.
- `batch[].queried`는 이번 조회 입력에 포함된 sha 표시 — batch 일부만
  횡전개 대상인데 동반 변경이 있는 위험한 케이스가 한눈에 드러난다.
- `removed_total > 0`은 gitlink 비전진 이동(reset·되돌림) 신호다.
- 회사 AI 정책에 따라 author 등 개발자 식별 정보는 싣지 않는다
  (sha·날짜·제목만). remote URL·경로·git stderr도 싣지 않는다.

exit code: `0`=성공 (sha별 실패는 `queries[].status`) / `2`=인자·검증 오류 /
`3`=repo 접근 오류. 실패 JSON에는 기계 판정용 `error_code`가 항상 포함된다.

## 사용법 — predecessors.py

excel에는 FTL sha만 적혀 있어서, 그 커밋이 의존하는 **선행 커밋이 횡전개
목록에서 누락**됐을 수 있다. `predecessors.py`는 주어진 FTL 커밋 `F`에 대해
"target branch에 아직 횡전개되지 않았으면서 `F`가 실제로 수정한 diff 부근에
blame으로 걸리는(연쇄 포함) 커밋"만 선행으로 판정해, `F`만 단독 pick하면
충돌하거나 조용히 깨질 상황을 사전에 드러낸다. 시간상 앞설 뿐 `F`의 변경
부근과 무관한 미반영 조상은 선행이 아니다 — 목록에 싣지 않고
`unrelated_unapplied_total`로 건수만 보고한다 (구간 전체의 미반영 목록이
필요하면 HTML 리포트의 통합 뷰·그래프 드릴다운을 쓴다).

> **Agent 필수 확인 사항:** source branch(`--source-branch`)와 더불어 **FTL target
> branch(`--target-branch`)도** 사용자가 명시하지 않았다면 실행 전에 질문한다.
> 추측 금지 규칙은 두 branch 모두에 적용된다.

```sh
python3 predecessors.py \
  --repo ~/work/integration \
  --source-branch origin/develop_XXX \
  --submodule Src/FTL \
  --ftl-repo ~/work/FTL \
  --target-branch origin/develop \
  a3f9c21
```

`--repo`/`--source-branch`/`--submodule`/`--ftl-repo`/`--input`/`--fetch`/`--limit`/
`--thorough`/`--output`/`--output-dir`/`--resume`은 `resolve_sha.py`와 같다
(질의별 파일명은 `<sha>.predecessors.json` — 위 "질의(commit) 단위 증분 저장" 참고).
`--target-branch`은 **FTL repo의** 횡전개
받는 쪽 branch(remote-tracking ref 권장)로, 반영 여부 판정의 기준점이다.
`--html PATH`를 주면 판정 결과를 담은 대화형 HTML 리포트도 함께 생성한다
(아래 참고).
`--sub-repo`는 받지 않는다 — 동반 gitlink 이동 여부는 integration tree에서
경로만 보고하고(`companions_moved`), 동반 커밋의 상세 세트는 해당 pegging을
`resolve_sha.py`로 후속 조회한다.

`--since DATE`(git 날짜 표현 — `1.year`, `2025-01-01` 등)는 **판정 창**을
제한한다. 분기가 오래된 branch 쌍에서는 patch 등가 스캔(분기 이후 양쪽 커밋
전부의 patch-id 계산)이 수십 분을 지배하는데, 창을 걸면 스캔이 창 내 커밋으로
줄어든다. 같은 창이 source·target 양쪽에 적용되어도 판정이 안전한 이유:
cherry-pick은 원본 커밋보다 나중에 기록되므로(committer date) **창 안의
커밋이 반영됐다면 그 pick도 반드시 창 안에 있다**. 대신 창 밖의 오래된
조상은 **미판정**으로 남으며 결과에 명시된다 — `window.excluded_total`
(창 밖이라 판정하지 않은 조상 수 전체)과 `queries[].window_clipped`(그
질의의 bloodline이 창 절단에 닿았는지). `window_clipped: true`면 창 내에
미반영이 없어도 "선행 없음 확정"이 아니다. 조회 sha 자체가 창 밖이면
`self.applied: "unknown"`으로 보고한다. 주의: committer date를 인위로
되돌린 이력(`git cherry-pick --committer-date-is-author-date` 등)에서는
창을 넉넉히 잡아야 한다.

### 판정 로직

1. **미반영 후보 추출** — 횡전개는 cherry-pick이라 target에는 다른 sha로
   존재하므로 ancestry만으로는 반영 여부를 알 수 없다. patch 등가
   (`rev-list --right-only --cherry-pick T...F`)로 "target에 패치 등가물이
   없는 `F`의 ancestor"만 남긴다. merge 커밋은 patch 등가 판정이 불가해
   목록에서 제외하고 `merges_skipped`로 건수만 보고한다 — 횡전개는
   fast-forward/rebase 전용이라 정상 이력에는 merge가 없어야 하며, HTML
   리포트는 0이면 표시하지 않고 발견 시에만 경고로 띄운다.
   이 스캔이 전체 비용을 지배하므로(분기 이후 **양쪽** 커밋 전부의
   patch-id 계산) 질의별로 반복하지 않는다 — 질의 sha들을 포함 관계 상
   최대인 sha 단위로 묶어 **한 번만 스캔**하고, 각 질의의 후보 집합은
   스캔 결과의 부모 그래프에서 복원한다. sha를 몇 개 넘기든 스캔 비용은
   거의 같으므로 일괄 호출이 항상 유리하다.
2. **IMS key 2차 판정** — 충돌 해소·squash로 변형된 pick은 patch-id가 어긋나
   거짓 미반영이 된다. 커밋 메시지의 IMS key(예: `AGCD-134`)는 횡전개 시
   유지되므로, target 쪽 메시지에서 같은 key가 발견되면 `applied_evidence:
   "ims_key"`로 표시한다(변형 반영 가능성 — 사람이 확인). key 하나가 커밋
   여러 개에 걸칠 수 있어 자동 제외하지는 않는다. key 형식은
   `--ims-pattern`으로 조정한다.
3. **선행 판정 (blame 기반 diff 부근 의존 연쇄)** — `F`가 고친 줄의 직전
   상태(`F^`)를 `git blame`으로 조사해, `F`의 변경 부근(±3줄)을
   **마지막으로 만든 커밋**을 찾는다. blame은 `F^` 좌표에서 수행하므로
   사이 커밋의 삽입·삭제로 줄 번호가 밀려도 판정이 어긋나지 않는다. 이
   blame에 지목된 **미반영** 커밋만 선행(`risk: "required_first"`)이다.
   blame은 줄의 마지막 수정 커밋만 지목하므로, 지목된 미반영 커밋의 변경
   부근을 다시 blame해 **연쇄 의존까지 목록에 포함**한다 — 각 항목의
   `required_by`가 어느 커밋의 부근에서 지목됐는지(`F` 또는 다른 선행)를
   보여준다. 이미 반영된 커밋에 닿으면 연쇄는 끝난다(target에 내용이
   있으므로). 시간상 앞설 뿐 diff 부근과 무관한 미반영 조상(같은 파일 먼
   부근 포함)은 선행이 아니다 — `unrelated_unapplied_total`로 건수만
   보고한다. `F`가 merge이거나 blame이 실패하면 의존 판정이 불가하므로
   미반영 조상 전체를 `risk: "unknown"`으로 보수적으로 나열한다. `F`가
   새로 추가한 파일은 old가 없어 blame 대상이 없다. 커밋별 diff·blame은
   질의와 무관하므로 한 번만 계산해 질의 간 공유하고(연쇄가 같은 커밋을
   여러 질의에서 만나도 재계산 없음), blame의 이력 걷기는 `target..F^`
   하한으로 분기 이후 구간으로 제한한다.
4. **배달 pegging 버킷팅** — 각 선행 커밋이 어느 pegging으로 배달됐는지,
   `F`와 `same_batch`인지, 그 pegging에서 다른 gitlink가 함께 움직였는지
   (`companions_moved`)를 표시한다. sibling gitlink의 전후 sha는
   `companion_links`(`{path, from, to}`)로 함께 싣는다 — 커밋 목록 상세는
   여전히 `resolve_sha.py` 후속 조회.

| 필드 | 값 | 의미 |
|---|---|---|
| `queries[].self.applied` | `not_applied` | `F` 자체가 target에 미반영 |
| | `patch_applied` | patch 등가물 존재 — 이미 횡전개됨 |
| | `key_matched` | patch는 다르지만 IMS key 흔적 — 변형 반영 가능성, 확인 필요 |
| | `in_target_history` | `F`가 target 이력에 그대로 포함 (merge 등) |
| | `unknown` | merge 커밋 등 판정 불가 |
| `predecessors[].applied_evidence` | `none` / `ims_key` | 미반영 확정 / key 흔적 있음(확인 필요) |
| `predecessors[].risk` | `required_first` | 변경 부근의 blame에 지목됨 (`overlap_paths`) — 먼저 pick 필요. `required_by`가 지목한 커밋(`F` 또는 다른 선행 — 연쇄)을 보여준다 |
| | `unknown` | `F`가 merge이거나 blame 실패 — 의존 판정 불가, 미반영 조상 전체를 보수적으로 나열 |
| `queries[].unrelated_unapplied_total` | 수 | 시간상 앞서지만 `F`의 diff 부근과 무관해 목록에서 뺀 미반영 조상 수 (의존 판정 불가면 `null`) |

`predecessors`는 오래된 순(pick 적용 순서)이고, patch 등가로 이미 반영된
ancestor는 목록에서 빠지는 대신 `applied_total`로 집계된다. `F`가 아직
`not_pegged`여도 ancestry 기준 판정은 계속되므로 배달 전 사전 점검에도 쓸 수
있다. `unrelated_unapplied_total > 0`이면 파일 의존은 없어도 간접 의존
(헤더·인터페이스 경유) 가능성은 남는다 — "안전 확정"으로 해석하지 않는다.
(구버전 저장 JSON의 `same_file`/`independent` risk는 HTML 재생성 시 그대로
렌더된다.)

### HTML 리포트 (`--html PATH`)

```sh
python3 predecessors.py ... --html report.html a3f9c21
```

stdout JSON은 그대로 두고, 판정 결과와 **전체 질의 구간 합집합의 커밋
그래프 한 벌(부모 edge + 커밋별 반영 상태)** 을 내장한 대화형 리포트를
추가로 쓴다. excel 한 판(`--input picks.csv`)을 통째로 넣는 규모를
전제로 설계돼 있다.

- **요약 타일(triage)** — 질의 전체 / 미반영 선행 있음 / 확인 필요 /
  단독 pick 가능 / 이미 반영됨 / FTL에 없음 건수가 상단에 나오고, 타일
  클릭으로 해당 상태만 필터링한다. sha·제목·IMS key 텍스트 검색도 있다.
- **미반영 커밋 통합 뷰** — 모든 질의에 걸친 미반영 커밋을 오래된 순
  (= pick 적용 순서)으로 한 번씩만 나열하고, 각 커밋이 **몇 개의 질의를
  막는지(blocking count)** 를 붙인다. "무엇을 먼저 pick하면 몇 건이
  풀리는가"가 바로 보이므로 사실상 작업 순서표다.
- **질의별 상세** — 배달 순서로 정렬해 같은 pegging끼리 그룹핑한 접이식
  섹션. 문제 있는 질의(미반영 선행·확인 필요·해석 불가)만 기본으로
  펼쳐진다.
- **조상 드릴다운** — 커밋(질의 sha·선행 커밋·통합 뷰 행)을 클릭하면 그
  커밋의 조상들이 각각 반영됐는지(미반영 / key 일치 — 확인 필요 /
  기반영(diff 동일) / merge) 패널로 보인다. 조상 탐색은 내장 그래프를
  브라우저에서 걷는 것이라 클릭할 때 git이 필요 없다 — 파일 하나를 그대로
  공유하면 된다.
- 그래프가 질의 간 **공유(합집합 한 벌)** 라 파일 크기는 질의 수와 거의
  무관하다. 최대 2,000노드까지 내장하고 초과 시 절단 경고를 표시한다.
  `--limit`으로 잘린 predecessors 목록과 달리 그래프 드릴다운은 상한까지
  전부 탐색 가능하다.
- 외부 리소스(CDN·폰트·이미지) 없이 inline CSS/JS만 사용한다. 리포트에
  실리는 정보는 stdout JSON과 같다(sha·날짜·제목·IMS key — author 등
  개발자 식별 정보 없음).
- 쓰기 실패 시 `REPORT_WRITE_FAILED`(exit 3)로 중단한다. stdout JSON에는
  리포트 경로를 싣지 않는다(로컬 경로 금지 정책).
- 리포트 렌더링(HTML 템플릿·파일 쓰기)은 `predecessors_viz.py` 모듈로
  분리되어 있다 — `predecessors.py`가 import하는 순수 렌더링 계층(git·분석
  로직 없음)이므로, 배포 시 두 파일을 **같은 폴더**에 함께 둔다. `--html`을
  쓰지 않는 실행은 이 모듈의 내용과 무관하다.

### 저장된 JSON에서 보고서 재생성 (`predecessors_viz.py` CLI)

```sh
python3 predecessors_viz.py result.json --html report.html
```

`--output`으로 저장된 **전체 결과 JSON**(`predecessors.py`·`analyze.py` 둘 다
가능)에서 보고서만 다시 만든다 — 분석(git 스캔)을 재실행하지 않으므로 수십
분짜리 판정 결과를 몇 초 만에 리포트로 바꿀 수 있고, 리포트 표현을 고친 뒤
재생성하는 반복도 싸다. stdout 요약(digest) JSON은 선행 상세가 없어 렌더할
수 없다(`INVALID_ARGUMENT`).

- 저장본에 공유 그래프가 있으면(분석 실행이 `--emit-graph --output`이었던
  경우) 통합 뷰·조상 드릴다운까지 완전한 보고서가 나온다. 그래프가 없으면
  **질의별 상세 테이블은 온전히** 렌더되고, 통합 뷰·드릴다운 자리에는 그
  사실이 경고로 표시된다 — 나중에 보고서를 만들 가능성이 있는 장시간 실행은
  `--emit-graph --output`으로 저장해 두는 것이 좋다.
- 재생성한 보고서에는 "저장된 결과 JSON에서 재생성" 표시와 `--since` 실행의
  판정 창 정보(`window`)가 함께 실린다.
- stdout에는 작은 결과 JSON만 남는다(`mode: "render"`,
  `graph_embedded` 등) — 정책에 따라 파일 경로는 싣지 않는다.

### 출력 (JSON, stdout)

```json
{
  "schema_version": 1, "ok": true, "mode": "predecessors",
  "branch": "origin/develop_XXX", "branch_tip": {"sha": "…", "short": "…"},
  "target": {"ref": "origin/develop", "sha": "…", "short": "…"},
  "queries": [
    {"input": "a3f9c21", "ftl_sha": "…", "subject": "…", "date": "…",
     "status": "found", "pegging": "…",
     "companion_links": [{"path": "Src/HAL", "from": "…", "to": "…"}],
     "self": {"applied": "not_applied", "ims_keys": ["AGCD-134"]},
     "predecessors": [
       {"sha": "…", "date": "…", "subject": "…",
        "pegging": "…", "same_batch": false,
        "ims_keys": ["AGCD-77"], "applied_evidence": "none",
        "risk": "required_first", "required_by": ["…"],
        "overlap_paths": ["src/foo.c"], "same_file_paths": [],
        "companions_moved": ["Src/HAL"],
        "companion_links": [{"path": "Src/HAL", "from": "…", "to": "…"}]}
     ],
     "predecessors_total": 1, "predecessors_truncated": false,
     "unrelated_unapplied_total": 4,
     "applied_total": 3, "merges_skipped": 0, "notes": []}
  ],
  "fetch": {"requested": false, "attempted": false,
            "status": "not_requested", "repositories": {}},
  "notes": []
}
```

exit code 계약은 `resolve_sha.py`와 같다 (`0`/`2`/`3`, 실패 JSON에
`error_code`).

`--output` 시 stdout digest는 질의별로 sha·제목·자신의 반영 상태에 더해
선행 커밋을 두 확신 수준으로 나눠 싣는다 — `predecessors_confirmed`(미반영
확정, `applied_evidence: "none"`)와 `predecessors_unconfirmed`(IMS key
흔적 — 확인 필요, `"ims_key"`). 두 목록의 합이 `--limit` 상한을 따르며
(최근 커밋 우선, 각 오래된 순), 각 항목에는 지목 경로(`required_by`)와
같이 배달된 sibling gitlink sha(`siblings`)가 붙고, 질의에는
`unrelated_unapplied_total`(diff 부근 무관이라 목록에서 뺀 미반영 조상 수)도
실린다. digest 스키마는 질의 상태와 무관하게 고정이다 — 모든 키가 항상
존재하고, 판정이 없으면 `null`, 비면 `[]`다 (`summary.by_status`도 status
전체 키를 항상 싣는다).

## 사용법 — analyze.py

"develop_XXX의 FTL 커밋 xxxxx부터 yyyyy까지 분석해달라"는 요청을 한 번에
처리하는 orchestration이다. FTL repo에서 구간(FROM..TO, **양끝 포함**)을
커밋 목록으로 펼친 뒤 `resolve_sha.py`(배달 pegging·동반 세트)와
`predecessors.py`(미반영 선행·기반영 여부)를 subprocess로 실행하고,
`--html`이면 두 결과를 합친 통합 보고서 한 장을 쓴다.

```sh
python3 analyze.py \
  --repo ~/work/integration \
  --source-branch origin/develop_XXX \
  --submodule Src/FTL \
  --ftl-repo ~/work/FTL \
  --target-branch origin/develop \
  --sub-repo Src/HAL=~/work/HAL \
  --fetch --html report.html \
  a3f9c21 77d0e4f          # FROM(오래된 쪽) TO(최신 쪽)
```

- 인자는 두 스크립트의 것을 그대로 전달한다 — `--sub-repo`는
  `resolve_sha.py`로, `--ims-pattern`·`--target-branch`·`--since`는
  `predecessors.py`로. branch 확인 규칙(`--source-branch`·`--target-branch` 추측 금지)도
  동일하다.
- `--output-dir`·`--resume`도 두 자식으로 전달된다 — 구간의 각 커밋마다
  `<sha>.resolve.json`·`<sha>.predecessors.json`이 판정이 끝나는 대로
  생기고, `--resume` 재실행은 tip·인수가 일치하는 저장본을 재사용해
  끊긴 구간 분석을 이어간다 (위 "질의(commit) 단위 증분 저장" 참고).
- FROM이 TO의 ancestor가 아니면 `INVALID_RANGE`, 구간이 `--max-range`
  (기본 100)를 넘으면 `RANGE_TOO_LARGE`로 중단한다.
- stdout은 통합 JSON 하나다: `{"mode": "analyze", "range": …,
  "resolve": <resolve 출력>, "predecessors": <predecessors 출력>}`.
  공유 그래프는 크기 때문에 stdout에 싣지 않고 보고서에만 내장한다
  (`predecessors.py --emit-graph`가 내부적으로 쓰인다).
- `--output PATH`를 주면 통합 JSON을 파일로 쓰고 stdout에는 두 자식의
  요약(집계 + sha별 digest)만 남긴다 — 구간 분석 출력은 특히 커서 stdout이
  잘리는 도구 환경에서는 이 옵션을 권장한다.
- 자식 스크립트가 실패하면(`FETCH_FAILED` 등) 그 `error_code`와 exit
  code를 그대로 전달하고 `stage` 필드로 어느 단계인지 보고한다.
- 통합 보고서는 predecessors 보고서(요약 타일·통합 뷰·질의별 상세·조상
  드릴다운)에 **"pegging·동반 세트 상세 (배달 단위)"** 섹션이 추가된
  형태다 — 각 pegging의 FTL batch(분석 구간 내 커밋 표시)와 동반
  gitlink의 커밋 목록까지 한 장에서 본다.

## 검증

```
python3 -m unittest discover -s tests -v
```

실제 git repo 픽스처(gitlink는 `update-index --cacheinfo 160000`으로 구성)를
만들어 CLI 계약 전체를 검증한다. `tests/test_predecessors.py`는 FTL repo 안에
source(main)·target(develop) branch를 함께 구성해 patch 등가 pick·변형 pick
(IMS key만 일치)·미반영을 각각 재현한다 — cherry-pick 픽스처는 committer
date를 바꿔 원본과 동일 sha가 되는 것을 막아야 한다.

### 실제 사내 repo 없이 gitlink 테스트하기

테스트 때문에 integration/FTL/HAL/Shared/FIL 원격 repo를 clone하거나 실제
submodule을 초기화할 필요는 없다. 테스트마다 임시 디렉터리에 작은 일반 Git
repo를 만들고, integration repo의 index에 다른 repo의 commit SHA를 `160000`
mode로 직접 넣으면 Git이 실제 submodule과 동일한 gitlink tree entry를 만든다.

```sh
# FTL/HAL/Shared/FIL에는 필요한 모양의 commit DAG를 먼저 만든다.
ftl_sha=$(git -C "$ftl_repo" rev-parse HEAD)

# integration에는 checkout/submodule 등록 없이 gitlink만 기록한다.
git -C "$integration_repo" update-index --add \
  --cacheinfo "160000,$ftl_sha,Src/FTL"
git -C "$integration_repo" commit -m "peg FTL"

# mode/type이 실제 gitlink인지 확인한다.
git -C "$integration_repo" ls-tree HEAD -- Src/FTL
# 160000 commit <sha>\tSrc/FTL
```

`tests/test_resolve.py`는 이 방식을 사용해 다음의 작은 이력 그래프를 한 번
구성하고 각 CLI 시나리오를 격리된 subprocess로 검증한다.

```text
FTL: f1 -- f2 -- f3 -- f4 -- f5 -- f6
HAL: h1 -- h2
Shared: s1 -- s2
FIL: i1 -- i2

integration: P1(Src/FTL=f1, Src/HAL=h1, Src/Shared=s1, Src/FIL=i1)
              -- P2(Src/FTL=f3)                         # batch pegging
              -- P3(Src/FTL=f4, Src/HAL=h2,
                    Src/Shared=s2, Src/FIL=i2)           # companion pegging
```

이 구조는 테스트를 빠르고 재현 가능하게 유지하면서도 `ls-tree`, `log -S`,
`diff-tree` 등 `resolve_sha.py`가 사용하는 Git 명령은 mock하지 않는다. 즉,
**integration tree의 gitlink 탐색**은 실제 Git으로 통합 테스트하고, commit
목록 조회에 필요한 FTL/HAL/Shared/FIL object database만 각각의 임시 repo로
제공한다. companion repo를 아예 제공하지 않는 경우도 별도 테스트하여,
checkout이 없어도 integration gitlink의 전후 SHA까지는 보고되는 계약을
보장한다.

새 edge case는 이 그래프에 pegging 하나를 추가하거나 별도 test class에서
새 임시 그래프를 만들면 된다. 특히 reset/비전진, submodule 추가·삭제, object
누락, `--limit`은 서로 상태가 섞이지 않도록 별도 그래프로 두는 편이 좋다.

## 로드맵

- `pick.py` — 판정 결과대로 FTL(+동반) cherry-pick 실행. `coupled` 세트는
  단독 pick을 차단(SOP gate)하고, 충돌 시 멈춰서 보고
- `check.py` — 구간의 SOP 준수 여부 감사 (동반 변경이 같은 pegging에 없는
  위반 탐지)
