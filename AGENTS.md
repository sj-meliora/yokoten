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
| "이 커밋이 의존하는(먼저 pick해야 하는) 선행 커밋은", "이 sha 단독으로 pick해도 되나", "이 sha 이미 target에 반영됐나", "횡전개 분석 보고서를 만들어달라"(`--html`) | `predecessors.py` |
| "구간의 미반영 커밋 **전체** 목록·pick 작업 순서표를 달라" | `predecessors.py`의 **통합 뷰** — 질의별 선행 목록이 아니라 `--html` 보고서로 답한다. §3 기본 인수는 보고서를 항상 생성하며, 과거 저장본만 있으면 `--emit-graph --output` 저장본에서 재생성한다 (§3) |
| 선행 커밋의 동반 세트 **상세**(커밋 목록) | `predecessors.py`로 pegging 확인 후 `resolve_sha.py` 후속 조회 |
| "xxxxx부터 yyyyy까지(구간) 분석해달라", "이 범위 통째로 보고서로" | `analyze.py` — 내부에서 위 두 스크립트를 실행하고 통합 보고서 생성 |
| "이미 만든(저장된) 결과 JSON으로 보고서만 다시 만들어달라" | `predecessors_viz.py` CLI — 분석 재실행 없음 (§3 끝 참고) |

- sha가 **구간(부터~까지)** 으로 주어지면 `analyze.py` 하나로 실행한다 —
  두 스크립트를 손으로 따로 돌리지 않는다. 반대로 낱개 sha 목록에는
  `analyze.py`를 쓰지 않는다(구간 전용).
- 어느 쪽인지 애매한 요청(예: "이 sha 분석해줘")은 실행 전에 목적을
  되묻는다 — 배달 pegging·동반 해석이면 `resolve_sha.py`, 선행·기반영
  여부면 `predecessors.py`.
- **"선행(predecessors)"의 의미**: 질의 커밋이 실제로 수정한 diff 부근에
  blame으로 의존이 걸리는(연쇄 포함) 미반영 커밋이다 — 시간상 앞선 미반영
  전체가 아니다. 시간상 앞설 뿐 부근 무관인 미반영 조상은 목록에 없고
  건수(`unrelated_unapplied_total`)로만 오므로, "전체 미반영 목록"류
  요청을 질의별 선행 목록으로 답하면 틀린다 — 통합 뷰로 답한다(위 표).
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
  --fetch --limit 20 --emit-graph --output <결과 JSON 파일> \
  --html <보고서 HTML 파일> \
  <sha> [<sha> ...]        # 의뢰받은 sha 전부를 이 한 번에
```

| 인수 | 첫 실행 | 이유·에스컬레이션 |
|---|---|---|
| `--fetch` | 항상 | stale checkout 판정은 거짓 `not_pegged`를 낳는다. `FETCH_FAILED`(exit 3)는 "최신 확인 불가 시 판정하지 않는다"는 의도된 중단 — `--fetch`를 빼고 우회하지 말고 원인(네트워크·remote 설정)을 해결하거나 보고한다 |
| sha 인자 | 전부 한 호출에 | 가장 비싼 patch 등가 스캔은 질의 전체를 묶어 한 번만 수행되므로, sha를 따로 실행하면 그 스캔이 sha 수만큼 반복된다 (fetch·pegging 열거도 마찬가지). excel export는 `--input`으로 파일째 넘긴다 |
| `--limit` | `20` | 비용이 큰 후보별 상세(blame·pegging 버킷팅)의 상한. 초과 시 **최근 N건만** 남긴다(오래된 쪽이 잘림 — 해석 주의는 §5). `*_total`은 절단과 무관하게 전체 수를 보고하므로 triage에는 손실이 없다. `predecessors_truncated: true`이고 전체 목록이 실제로 필요할 때만 올려서 재실행한다. `0`(무제한)은 사용자가 명시할 때만 |
| `--output` | 항상 (임시 작업 파일 경로) | 수십 분 걸린 결과가 도구 stdout 제한(truncation)에 잘리면 통째로 유실된다. `--output`이면 전체 JSON은 파일로 가고 stdout에는 요약(`summary` + sha별 digest)만 남는다. digest에는 선행 커밋 목록이 확정/미확정으로 나뉘어 sibling sha와 함께 실리므로(§5) 대부분 digest만으로 요약할 수 있다 — blame 근거(`overlap_paths` 등)·절단된 전체 목록 같은 상세만 파일에서 **필요한 부분만** 조회한다(파일 전체를 다시 context로 읽지 않는다) |
| `--emit-graph` | 항상 (`--output`과 함께) | 구간 전체 미반영 목록(통합 뷰·조상 드릴다운)은 공유 그래프에만 있다(§1·§5). `--html` 보고서를 항상 만들더라도 그래프를 `--output` 저장본에 함께 남겨야, 보고서 파일 유실·표현 수정 시 `predecessors_viz.py`로 **재분석 없이** 완전한 보고서를 다시 만들 수 있다. 추가 비용 없음(그래프 수집은 `--html`과 공유) |
| `--thorough` | 쓰지 않음 | pegging 전수 선형 스캔이라 훨씬 느리다. notes에 "비전진 이력 감지"가 나오거나 사용자가 요구할 때만 |
| `--since` | 쓰지 않음 | 판정 창 제한(예: `--since 1.year`)은 §4의 지배 비용(patch 등가 스캔)을 실제로 줄이는 유일한 인수지만, **창 밖 조상은 미판정**이 된다. 사용자가 창을 명시했거나("최근 1년만", "직전 횡전개 이후만"), §4 측정 결과 오래 걸릴 규모라 소요 시간과 함께 제안해 합의했을 때만 쓴다 — 임의로 걸지 않는다 |
| `--html` | 항상 (보고서 HTML 파일 경로) | **통합 뷰(구간 전체 미반영 커밋 + blocking count = pick 작업 순서표)·조상 드릴다운은 이 보고서에서 본다** — 질의별 `predecessors`는 diff 부근 의존만 싣으므로 보고서가 전체 그림을 담는 기본 산출물이다. 비용(그래프 수집·target key 대조)은 `--emit-graph`와 같은 한 번이라 §4의 지배 비용 대비 작다. `analyze.py`도 항상 `--html`을 준다 — 그래프를 `--output` 저장본에 넣지 않으므로 실행 시점에 빠뜨리면 통합 뷰를 재분석 없이는 만들 수 없다 |
| `--max-range` (`analyze.py`) | 기본값 유지 | `RANGE_TOO_LARGE`면 임의로 올리지 말고, §4의 규모 측정을 근거로 소요 시간을 알린 뒤 상한을 올릴지 구간을 나눌지 사용자와 정한다 |

`resolve_sha.py`도 같은 기본값(`--fetch`, sha 일괄, `--limit`, `--output`)을
따른다 (`--emit-graph`는 `predecessors.py` 전용 — `resolve_sha.py`·
`analyze.py`에는 없다).

**보고서만 다시 필요하면 분석을 재실행하지 않는다** — `--output` 저장본이
있으면 `python3 predecessors_viz.py <저장 JSON> --html report.html`로
재생성한다(몇 초, git 접근 없음). digest(stdout 요약)로는 안 되고 전체 결과
파일이어야 한다. §3 기본 인수(`--emit-graph --output`)대로 저장한 파일이면
통합 뷰·드릴다운까지 완전한 보고서가 나온다. 그래프 없는 저장본(기본 인수
이전 실행분·`analyze.py --output`)은 질의별 상세만 렌더되고 통합 뷰·
드릴다운은 빠진다 — 통합 뷰까지 필요하면 재분석 없이는 못 만든다고
사용자에게 알리고 범위를 정한다.
`--sub-repo`는 `resolve_sha.py` 전용이고(`analyze.py`는 받아서 전달),
integration checkout의 해당 submodule이 미초기화(빈 폴더)일 때만 필요하다 —
README의 "`--sub-repo`가 필요한 경우" 표 참고. `predecessors.py`는
`--sub-repo`를 받지 않는다 — 동반 gitlink는 경로(`companions_moved`)와
전후 sha(`companion_links`, digest에서는 `siblings`)로 보고하며, 동반
세트 **상세(커밋 목록)** 는 `resolve_sha.py`로 후속 조회한다.

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
- 선행 판정은 blame 기반 **diff 부근 의존**만 싣는다: F의 변경 부근(±3줄)을
  마지막으로 만든 미반영 커밋과, 그 커밋의 변경 부근에서 연쇄로 지목되는
  미반영 커밋만 `predecessors`에 오른다(`risk`는 `required_first`,
  `required_by`가 지목 경로 — F가 아니면 연쇄 의존). F가 merge이거나 blame이
  실패하면 미반영 조상 전체가 `risk: "unknown"`으로 나열된다(의존 판정 불가 —
  선행 확정으로 요약하지 않는다). 시간상 앞설 뿐 diff 부근과 무관한 미반영
  조상은 목록에 없고 `unrelated_unapplied_total`로 건수만 온다 — 파일 의존이
  없어도 간접 의존(헤더·인터페이스 경유) 가능성은 남으므로, 이 수가 0이
  아니면 요약에서 생략하지 말고 "안전 확정"으로도 표현하지 않는다. 사용자가
  부근 무관까지 포함한 전체 미반영 목록을 원하면 통합 뷰로 안내한다 —
  §3 기본 인수가 항상 생성하는 `--html` 보고서이고, 과거 저장본만 있으면
  `--emit-graph --output` 저장본에서 `predecessors_viz.py`로 재생성한다.
- digest의 `predecessors_confirmed`는 미반영 **확정**(`applied_evidence:
  "none"`), `predecessors_unconfirmed`는 IMS key 흔적만 있는 **미확정**
  (`"ims_key"` — 위 확인 필요 규칙 그대로)이다. 두 목록의 합이 `--limit`
  상한을 따르고, digest 스키마는 질의 상태와 무관하게 고정이다(판정 없음
  `null`, 빈 결과 `[]`).
- blame은 각 줄의 마지막 수정 커밋만 지목하므로 더 오래된 의존은
  `required_by` 연쇄로 목록에 포함된다 — 미반영 선행은 반드시 보고서의
  순서(오래된 순 = pick 적용 순서)대로 처리하도록 안내한다.
- `--limit` 절단은 **최근 N건**을 남긴다 — `predecessors_truncated:
  true`면 잘려나간 것은 목록 맨 앞보다 **더 오래된** 선행들이다 (출력
  파일의 `predecessors`도 같은 절단을 따른다). 절단된 목록만 보고 pick
  순서를 안내하지 않는다 — limit을 올려 재실행한 뒤 안내한다.
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
  `predecessors.py`에). 저장된 JSON에서 보고서를 재생성하는 CLI도 이 경계
  안(파일 읽기 → HTML 쓰기)에서만 동작한다. `tests/test_viz.py`가 이
  경계를 검증한다.
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
