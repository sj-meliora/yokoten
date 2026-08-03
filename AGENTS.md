# Agent 운영 규칙

이 repo에서 작업하는 모든 agent(Claude Code, Codex 등)가 따르는 규칙이다.
Claude Code는 이 파일을 `CLAUDE.md`의 import로 읽는다 — 규칙 본문은 이 파일
하나로만 관리한다.

## 1. 어떤 요청에 어떤 스크립트인가

FTL 커밋 sha를 놓고 횡전개 관련 **판정**을 요구하는 요청이면 아래 스크립트가
담당이다. git 명령(rev-list·blame·`log -S` 등)을 직접 조합해 수동으로
판정하지 않는다 — fetch 규칙·판정 로직·출력 정책은 스크립트가 보장한다.

| 요청 신호 (예) | 담당 |
|---|---|
| "이 FTL sha 어느 pegging으로 배달됐나", "같이 반영해야 하는 HAL/Shared/FIL(동반) 커밋은", "excel의 sha들을 batch 단위로 묶어달라" | `resolve_sha.py` |
| "이 커밋보다 먼저 횡전개됐어야 하는(선행) 커밋은", "이 sha 단독으로 pick해도 되나", "이 sha 이미 target에 반영됐나", "횡전개 분석 보고서를 만들어달라"(`--html`) | `predecessors.py` |
| 선행 커밋의 동반 세트 **상세**(커밋 목록) | `predecessors.py`로 pegging 확인 후 `resolve_sha.py` 후속 조회 |
| "xxxxx부터 yyyyy까지(구간) 분석해달라", "이 범위 통째로 보고서로" | `analyze.py` — 내부에서 위 두 스크립트를 실행하고 통합 보고서 생성 |

- 표에 해당하지 않는 일반 git 질문·코드 수정 요청에는 스크립트를 억지로
  쓰지 않는다.
- 어느 쪽인지 애매한 요청(예: "이 sha 분석해줘")은 실행 전에 목적을
  되묻는다 — 배달 pegging·동반 해석이면 `resolve_sha.py`, 선행·기반영
  여부면 `predecessors.py`. sha가 **구간(부터~까지)** 으로 주어지면
  `analyze.py` 하나로 실행한다 — 두 스크립트를 손으로 따로 돌리지 않는다.

## 2. Branch는 추측하지 말고 사용자에게 확인한다

위 표에 해당하는 요청에서:

- 사용자가 source integration branch를 명시하지 않았다면, git 탐색·fetch·
  스크립트 실행 **전에** `develop`인지 정확히 어떤 `develop_XXX`인지 먼저
  질문한다.
- 문서 예시, repo 이름, remote branch 목록, 과거 작업을 근거로 branch를
  추측하지 않는다. 그럴듯한 `develop_*`를 검색해서 대신 고르지도 않는다.
- 사용자가 이미 명확히 지정했다면 다시 묻지 않는다.
- `--branch`에는 로컬 branch가 아니라 remote-tracking ref(`origin/develop`,
  `origin/develop_XXX`)를 넘긴다 — 로컬 branch는 fetch해도 자동으로 갱신되지
  않는다.
- `predecessors.py`의 `--target`(횡전개 반영 여부 판정 기준이 되는 FTL
  target branch)도 같은 규칙을 따른다 — 명시되지 않았으면 실행 전에 질문하고,
  remote-tracking ref를 넘긴다.

## 3. 스크립트 실행 규칙

- 판정 목적의 실행에는 항상 `--fetch`를 붙인다. stale checkout으로 판정하면
  거짓 `not_pegged`가 나온다.
- `FETCH_FAILED`(exit 3)는 "최신 상태를 확인할 수 없으니 판정하지 않는다"는
  의도된 중단이다. `--fetch`를 빼고 재실행해서 우회하지 말고, 원인(네트워크·
  remote 설정)을 해결하거나 사용자에게 보고한다.
- `--sub-repo`는 `resolve_sha.py` 전용이고(`analyze.py`는 받아서
  `resolve_sha.py`로 전달), integration checkout의 해당
  submodule이 미초기화(빈 폴더)일 때만 필요하다. 초기화된 submodule은
  자동으로 사용된다 — README의 "`--sub-repo`가 필요한 경우" 표 참고.
  `predecessors.py`는 `--sub-repo`를 받지 않는다 — 동반 gitlink는 경로만
  보고하며(`companions_moved`), 동반 세트 상세는 `resolve_sha.py`로 후속
  조회한다.
- 결과 해석: exit 0이어도 sha별 판정은 `queries[].status`로 확인한다. 실패
  JSON의 기계 판정 키는 `error_code`다.

## 4. predecessors.py 결과 해석 규칙

사용자에게 결과를 요약할 때 판정의 확신 수준을 뒤섞지 않는다:

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

## 5. 코드 수정 규칙

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
- 동작 변경에는 해당 스크립트의 테스트(`tests/test_resolve.py`·
  `tests/test_predecessors.py`·`tests/test_analyze.py`·`tests/test_viz.py`)에
  회귀 테스트를 추가하고 `python3 -m unittest discover -s tests`가
  통과해야 한다.
