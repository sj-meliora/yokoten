# yokoten

횡전개(橫展開, [yokoten](https://en.wikipedia.org/wiki/Toyota_Production_System))
지원 도구 모음. 사업화 branch(예: `develop_Evan`)에서 개발된 변경점을 개발
branch(`develop`)로 주기적으로 cherry-pick하는 업무를 돕는다. 기능 하나가
스크립트 하나다:

| 스크립트 | 역할 |
|---|---|
| `resolve_sha.py` | FTL sha → 배달 pegging 역추적 + **같이 반영되어야 하는 HAL/Shared 커밋** 해석 |

Python 3.10+ 표준 라이브러리와 git CLI만 사용한다 (외부 의존성 없음).

## 배경

- 횡전개 대상은 excel로 관리되며, 거기에는 **FTL 기준 sha**만 적혀 있다.
- FTL 변경은 integration repo에 submodule gitlink 갱신(pegging)으로 반영되고,
  **batch로 pegging되는 경우가 많다** — 특정 FTL sha와 1:1 대응하는 pegging이
  없을 수 있다.
- **SOP: FTL과 엮인 HAL/Shared 변경은 반드시 같은 pegging 커밋에 함께
  반영된다.** 따라서 배달 pegging에서 다른 gitlink가 움직이지 않았다면
  "동반 변경 없음"이 확정이고, 움직였다면 그것이 동반 후보의 전부다.

## 사용법 — resolve_sha.py

```
resolve_sha.py --repo <integration clone> --branch origin/develop_Evan \
               [--submodule FTL] [--ftl-repo DIR] [--sub-repo HAL=DIR ...] \
               [--fetch] [--limit N] [--thorough] \
               <FTL_SHA> [<FTL_SHA> ...]            # 또는 --input picks.csv
```

- sha 여러 개를 한 번에 넘기면 **pegging 단위로 그룹핑**되어 나온다 — 같은
  batch로 배달된 sha들은 한 세트로 판단할 수 있다.
- `--input`은 excel export(CSV/텍스트)를 그대로 받는다. 각 줄의 첫 필드가
  sha면 수집하고, 헤더 등 나머지 줄은 무시 후 `notes`에 집계한다.
- `--sub-repo PATH=DIR`로 HAL/Shared repo 위치를 지정한다. 미지정 시
  `<repo>/<PATH>`의 초기화된 submodule을 시도하고, 둘 다 없으면 gitlink
  전후 sha까지만 보고한다.

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
  "branch": "origin/develop_Evan", "branch_tip": {"sha": "…", "short": "…"},
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
  "fetch": {"requested": false, "attempted": false, "status": "not_requested"},
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

## 로드맵

- `pick.py` — 판정 결과대로 FTL(+동반) cherry-pick 실행. `coupled` 세트는
  단독 pick을 차단(SOP gate)하고, 충돌 시 멈춰서 보고
- `check.py` — 구간의 SOP 준수 여부 감사 (동반 변경이 같은 pegging에 없는
  위반 탐지)
