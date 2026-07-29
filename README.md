# yokoten

횡전개(橫展開, [yokoten](https://en.wikipedia.org/wiki/Toyota_Production_System))
지원 도구 모음. 확인된 사업화 branch(예: `develop_XXX`)에서 개발된 변경점을 개발
branch(`develop`)로 주기적으로 cherry-pick하는 업무를 돕는다. 기능 하나가
스크립트 하나다:

| 스크립트 | 역할 |
|---|---|
| `resolve_sha.py` | FTL sha → 배달 pegging 역추적 + **같이 반영되어야 하는 HAL/Shared/FIL 커밋** 해석 |

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
resolve_sha.py --repo <integration clone> --branch origin/<CONFIRMED_BRANCH> \
               [--submodule PATH] [--ftl-repo DIR] [--sub-repo PATH=DIR ...] \
               [--fetch] [--limit N] [--thorough] \
               <FTL_SHA> [<FTL_SHA> ...]            # 또는 --input picks.csv
```

실제 `Src/*` 배치에서는 다음처럼 지정한다.

```sh
python3 resolve_sha.py \
  --repo ~/work/integration \
  --branch origin/develop_XXX \
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
| `--branch` | `origin/develop_XXX` | 사용자에게 확인한 pegging 조회 integration branch |
| `--submodule` | `Src/FTL` | integration **tree 안에서의** FTL gitlink 경로 |
| `--ftl-repo` | `~/work/FTL` | ancestor/batch 조회에 사용할 FTL **로컬 clone** |
| `--sub-repo` | `Src/FIL=~/work/FIL` | `gitlink 경로=로컬 clone 경로`; 필요한 만큼 반복 |
| `--fetch` |  | 판정 전에 integration·FTL·지정 companion의 `origin`을 함께 갱신; 하나라도 실패하면 판정 중단 |
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
상황을 막으려면 자동화 호출에 `--fetch`를 사용해야 한다. `--branch`에는 fetch로
갱신되는 `origin/develop_XXX` 같은 remote-tracking ref를 권장한다(로컬 branch는
fetch해도 자동 fast-forward되지 않는다).

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

## 검증

```
python3 -m unittest discover -s tests -v
```

실제 git repo 픽스처(gitlink는 `update-index --cacheinfo 160000`으로 구성)를
만들어 CLI 계약 전체를 검증한다.

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
