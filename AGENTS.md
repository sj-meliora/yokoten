# Agent 운영 규칙

이 repo에서 작업하는 모든 agent(Claude Code, Codex 등)가 따르는 규칙이다.
Claude Code는 이 파일을 `CLAUDE.md`의 import로 읽는다 — 규칙 본문은 이 파일
하나로만 관리한다.

## 1. Source branch는 추측하지 말고 사용자에게 확인한다

`resolve_sha.py`가 필요한 요청에서:

- 사용자가 source integration branch를 명시하지 않았다면, git 탐색·fetch·
  스크립트 실행 **전에** `develop`인지 정확히 어떤 `develop_XXX`인지 먼저
  질문한다.
- 문서 예시, repo 이름, remote branch 목록, 과거 작업을 근거로 branch를
  추측하지 않는다. 그럴듯한 `develop_*`를 검색해서 대신 고르지도 않는다.
- 사용자가 이미 명확히 지정했다면 다시 묻지 않는다.
- `--branch`에는 로컬 branch가 아니라 remote-tracking ref(`origin/develop`,
  `origin/develop_XXX`)를 넘긴다 — 로컬 branch는 fetch해도 자동으로 갱신되지
  않는다.

## 2. 스크립트 실행 규칙

- 판정 목적의 실행에는 항상 `--fetch`를 붙인다. stale checkout으로 판정하면
  거짓 `not_pegged`가 나온다.
- `FETCH_FAILED`(exit 3)는 "최신 상태를 확인할 수 없으니 판정하지 않는다"는
  의도된 중단이다. `--fetch`를 빼고 재실행해서 우회하지 말고, 원인(네트워크·
  remote 설정)을 해결하거나 사용자에게 보고한다.
- `--sub-repo`는 integration checkout의 해당 submodule이 미초기화(빈 폴더)일
  때만 필요하다. 초기화된 submodule은 자동으로 사용된다 — README의
  "`--sub-repo`가 필요한 경우" 표 참고.
- 결과 해석: exit 0이어도 sha별 판정은 `queries[].status`로 확인한다. 실패
  JSON의 기계 판정 키는 `error_code`다.

## 3. 코드 수정 규칙

- 외부 의존성 금지 — Python 3.10+ 표준 라이브러리와 git CLI만 사용한다.
- 회사 AI 정책: stdout JSON에 developer 식별 정보를 싣지 않는다(sha·날짜·
  제목만 허용). remote URL·로컬 경로·git stderr도 싣지 않는다.
- 동작 변경에는 `tests/test_resolve.py`에 회귀 테스트를 추가하고
  `python3 -m unittest discover -s tests`가 통과해야 한다.
