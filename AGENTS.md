# Agent 운영 규칙

이 repo에서 작업하는 모든 agent(Claude Code, Codex 등)가 따르는 규칙이다.
Claude Code는 이 파일을 `CLAUDE.md`의 import로 읽는다 — 규칙 본문은 이 파일
하나로만 관리한다.

작업 순서 요약: **① 요청을 §1 표로 라우팅 → ② branch 두 개를 사용자에게
확인(§2) → ③ §3의 기본 인수로 실행하되 오래 걸릴 규모면 먼저 예고(§4) →
④ §5의 확신 수준을 지켜 요약**. 코드 수정·검증은 §6.

## 1. 요청 라우팅 — 어떤 요청에 어떤 스크립트인가

FTL 커밋 sha를 놓고 횡전개 관련 **판정**을 요구하는 요청이면 아래 스크립트가
담당이다. git 명령(rev-list·blame·`log -S` 등)을 직접 조합해 수동으로
판정하지 않는다 — fetch 규칙·판정 로직·출력 정책은 스크립트가 보장한다.
(판정이 아닌 read-only 규모 측정은 무방하다 — §4.)

| 요청 신호 (예) | 담당 |
|---|---|
| "이 FTL sha 어느 pegging으로 배달됐나", "같이 반영해야 하는 HAL/Shared/FIL(동반) 커밋은", "excel의 sha들을 batch 단위로 묶어달라" | `resolve_sha.py` |
| "이 커밋보다 먼저 횡전개됐어야 하는(선행) 커밋은", "이 sha 단독으로 pick해도 되나", "이 sha 이미 target에 반영됐나", "횡전개 분석 보고서를 만들어달라"(`--html`) | `predecessors.py` |
| 선행 커밋의 동반 세트 **상세**(커밋 목록) | `predecessors.py`로 pegging 확인 후 `resolve_sha.py` 후속 조회 |
| "xxxxx부터 yyyyy까지(구간) 분석해달라", "이 범위 통째로 보고서로" | `analyze.py` — 내부에서 위 두 스크립트를 실행하고 통합 보고서 생성 |

- sha가 **구간(부터~까지)** 으로 주어지면 `analyze.py` 하나로 실행한다 —
  두 스크립트를 손으로 따로 돌리지 않는다. 반대로 낱개 sha 목록에는
  `analyze.py`를 쓰지 않는다(구간 전용).
- 어느 쪽인지 애매한 요청(예: "이 sha 분석해줘")은 실행 전에 목적을
  되묻는다 — 배달 pegging·동반 해석이면 `resolve_sha.py`, 선행·기반영
  여부면 `predecessors.py`.
- 표에 해당하지 않는 일반 git 질문·코드 수정 요청에는 스크립트를 억지로
  쓰지 않는다.

## 2. branch는 추측하지 말고 사용자에게 확인한다

- `--source-branch`(source integration branch)와 `--target-branch`(FTL target branch —
  `predecessors.py`·`analyze.py`)이 명시되지 않았으면 git 탐색·fetch·스크립트
  실행 **전에** 질문한다. `develop`인지 정확히 어떤 `develop_XXX`인지까지
  확인한다.
- 문서 예시, repo 이름, remote branch 목록, 과거 작업을 근거로 추측하지
  않는다. 그럴듯한 `develop_*`를 검색해서 대신 고르지도 않는다. 사용자가
  이미 명확히 지정했다면 다시 묻지 않는다.
- 두 인자 모두 로컬 branch가 아니라 remote-tracking ref(`origin/develop`,
  `origin/develop_XXX`)를 넘긴다 — 로컬 branch는 fetch해도 자동으로 갱신되지
  않는다.

## 3. 실행 인수 기본값 — 첫 실행은 이 형태로

```sh
python3 predecessors.py \
  --repo <integration clone> --source-branch origin/<확인한 branch> \
  --submodule <gitlink 경로> --ftl-repo <FTL clone> \
  --target-branch origin/<확인한 target> \
  --fetch --limit 20 --output <결과 JSON 파일> \
  <sha> [<sha> ...]        # 의뢰받은 sha 전부를 이 한 번에
```

| 인수 | 첫 실행 | 이유·에스컬레이션 |
|---|---|---|
| `--fetch` | 항상 | stale checkout 판정은 거짓 `not_pegged`를 낳는다. `FETCH_FAILED`(exit 3)는 "최신 확인 불가 시 판정하지 않는다"는 의도된 중단 — `--fetch`를 빼고 우회하지 말고 원인(네트워크·remote 설정)을 해결하거나 보고한다 |
| sha 인자 | 전부 한 호출에 | 가장 비싼 patch 등가 스캔은 질의 전체를 묶어 한 번만 수행되므로, sha를 따로 실행하면 그 스캔이 sha 수만큼 반복된다 (fetch·pegging 열거도 마찬가지). excel export는 `--input`으로 파일째 넘긴다 |
| `--limit` | `20` | 비용이 큰 후보별 상세(blame·pegging 버킷팅)의 상한. `*_total`은 절단과 무관하게 전체 수를 보고하므로 triage에는 손실이 없다. `predecessors_truncated: true`이고 전체 목록이 실제로 필요할 때만 올려서 재실행한다. `0`(무제한)은 사용자가 명시할 때만 |
| `--output` | 항상 (임시 작업 파일 경로) | 수십 분 걸린 결과가 도구 stdout 제한(truncation)에 잘리면 통째로 유실된다. `--output`이면 전체 JSON은 파일로 가고 stdout에는 요약(`summary` + sha별 digest)만 남는다 — 요약으로 triage하고, 선행 커밋 목록 같은 상세는 파일에서 **필요한 부분만** 조회한다(파일 전체를 다시 context로 읽지 않는다) |
| `--thorough` | 쓰지 않음 | pegging 전수 선형 스캔이라 훨씬 느리다. notes에 "비전진 이력 감지"가 나오거나 사용자가 요구할 때만 |
| `--since` | 쓰지 않음 | 판정 창 제한(예: `--since 1.year`)은 §4의 지배 비용(patch 등가 스캔)을 실제로 줄이는 유일한 인수지만, **창 밖 조상은 미판정**이 된다. 사용자가 창을 명시했거나("최근 1년만", "직전 횡전개 이후만"), §4 측정 결과 오래 걸릴 규모라 소요 시간과 함께 제안해 합의했을 때만 쓴다 — 임의로 걸지 않는다 |
| `--html` | 사용자가 보고서를 원할 때만 | 그래프 수집·target key 대조 비용이 추가된다 |
| `--max-range` (`analyze.py`) | 기본값 유지 | `RANGE_TOO_LARGE`면 임의로 올리지 말고, §4의 규모 측정을 근거로 소요 시간을 알린 뒤 상한을 올릴지 구간을 나눌지 사용자와 정한다 |

`resolve_sha.py`도 같은 기본값(`--fetch`, sha 일괄, `--limit`, `--output`)을
따른다.
`--sub-repo`는 `resolve_sha.py` 전용이고(`analyze.py`는 받아서 전달),
integration checkout의 해당 submodule이 미초기화(빈 폴더)일 때만 필요하다 —
README의 "`--sub-repo`가 필요한 경우" 표 참고. `predecessors.py`는
`--sub-repo`를 받지 않는다 — 동반 gitlink는 경로만 보고하며
(`companions_moved`), 동반 세트 상세는 `resolve_sha.py`로 후속 조회한다.

## 4. 실행 시간 — 예고하고, 기다리고, 재시작하지 않는다

소요 시간을 지배하는 비용은 `--limit`으로 줄일 수 없는 것들이다:

- **patch 등가 스캔**(`predecessors.py`) — source·target이 분기한 뒤 쌓인
  **양쪽 커밋 전부**의 patch-id 계산. 분기가 오래된 branch 쌍이면 sha
  하나에도 수십 분이 정상일 수 있다.
- **pickaxe·pegging 열거**(`resolve_sha.py`) — sha당 integration
  first-parent 이력을 한 번씩 훑는다.

따라서:

1. 실행 전에 규모를 잰다(판정이 아니라 측정이므로 §1 위반이 아니다):
   `git rev-list --count <target>...<sha>`. 수천 건 이상이면 "수십 분 걸릴
   수 있다"고 사용자에게 먼저 알리고 실행한다.
2. 오래 걸릴 실행은 background로 돌리고 완료를 기다린다. 진행 중인 실행을
   죽이고 같은 명령을 다시 시작하지 않는다 — 스캔이 처음부터 다시 돈다.
3. 타임아웃 등으로 끊겼으면 인수만 바꿔 무작정 재시도하지 말고, 1의
   측정치와 함께 상황을 보고하고 사용자와 범위를 조정한다.

## 5. 결과 해석 — 확신 수준을 뒤섞지 않는다

- exit 0이어도 sha별 판정은 `queries[].status`로 확인한다. 실패 JSON의
  기계 판정 키는 `error_code`다.
- `patch_applied`(기반영, diff 동일)만 "이미 횡전개됨" **확정**이다.
  `key_matched`/`applied_evidence: "ims_key"`는 "IMS key 흔적이 있으니
  사람이 확인해야 한다"는 뜻이다 — 반영 완료로 요약하면 안 된다 (key
  하나가 커밋 여러 개에 걸칠 수 있다).
- `risk`는 blame 기반이다: `required_first`는 F의 변경 부근(±3줄)을
  마지막으로 만든 커밋으로 지목됐다는 **직접 의존**이고, `same_file`은
  "같은 파일이지만 변경 부근 아님"인 참고 등급이다. `independent`도
  간접 의존(헤더·인터페이스 경유) 가능성은 남으므로 "안전 확정"으로
  표현하지 않는다.
- blame은 각 줄의 마지막 수정 커밋만 지목한다 — 미반영 선행은 반드시
  보고서의 순서(오래된 순 = pick 적용 순서)대로 처리하도록 안내한다.
- `merges_skipped > 0`은 fast-forward/rebase 전용 흐름 위반 신호다 —
  요약에서 생략하지 말고 사용자에게 알린다.
- `--since` 실행에서 `window_clipped: true`는 "창 밖 조상은 판정하지
  않았다"는 뜻이다 — 창 내에 미반영이 없어도 "선행 없음 확정"으로
  요약하지 않고, `window.excluded_total`(미판정 조상 수)을 함께 알린다.

## 6. 코드 수정·검증 규칙

- 외부 의존성 금지 — Python 3.10+ 표준 라이브러리와 git CLI만 사용한다.
  HTML 리포트도 외부 리소스(CDN·폰트·이미지) 없이 self-contained를
  유지한다.
- 회사 AI 정책: stdout JSON에 developer 식별 정보를 싣지 않는다(sha·날짜·
  제목·IMS key만 허용). remote URL·로컬 경로·git stderr도 싣지 않는다.
  `--html` 리포트에 실리는 정보도 stdout JSON과 같은 범위를 지킨다.
- 모듈 경계: `predecessors_viz.py`는 순수 렌더링 계층이다 — subprocess·
  git 실행·`resolve_sha` import를 넣지 않는다 (분석·git 접근은
  `predecessors.py`에). `tests/test_viz.py`가 이 경계를 검증한다.
- stdout JSON의 필드명·상태값(`applied_total`, `patch_applied` 등)은
  기계 소비 계약이다 — 표현을 바꾸고 싶으면 `predecessors_viz.py`의 UI
  라벨만 바꾸고, JSON 스키마 변경은 필드 추가(additive)로만 한다.

검증은 변경의 성격에 맞게 한다:

| 변경 | 요구 검증 |
|---|---|
| 판정·출력 **동작 변경** | 해당 스크립트의 테스트 파일(`tests/test_resolve.py`·`tests/test_predecessors.py`·`tests/test_analyze.py`·`tests/test_viz.py`)에 회귀 테스트 추가 + `python3 -m unittest discover -s tests` 통과 |
| 문서·주석·메시지 문자열·인코딩 등 **비동작 변경** | 기존 스위트 1회 통과로 충분 — 새 픽스처·새 테스트를 만들지 않는다 |

- 스위트는 임시 디렉터리에 작은 repo 픽스처를 만들어 도는 구조로 10초
  안에 끝난다 (README "실제 사내 repo 없이 gitlink 테스트하기" 참고).
  이보다 무거운 검증 절차를 즉석에서 설계하지 않는다.
- **실제 사내 repo(integration/FTL/HAL/Shared/FIL 등)를 검증에 쓰지
  않는다** — branch·commit 생성, push, config 변경 등 어떤 쓰기도 금지다.
  실제 repo에는 read-only 판정 실행만 한다.
