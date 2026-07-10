# Plan: Action Log 프롬프트 토큰 절감 구현

- Status: Draft
- Date: 2026-07-10
- Spec: `docs/specs/2026-07-10-action-log-prompt-token-reduction.md`
- 대상 파일: `autoresearch/action_logs/llm_generator.py`,
  `autoresearch/action_logs/pipeline.py`, `autoresearch/action_logs/schema.py`
- 테스트: `tests/test_action_logs_llm_generator.py`,
  `tests/test_action_logs_pipeline.py`, `tests/test_action_logs_daily.py`

## Step 0 — 이슈·브랜치 (필수 선행)

- 코드 변경 작업이므로 이슈를 먼저 발행하고, 이슈의 `Create a branch`로 브랜치
  생성(이슈-브랜치 자동 연결). 현재 브랜치(`fix/101-...`)와 무관하므로 신규
  브랜치에서 진행한다.
- 구조 변경(파싱 리팩터)과 동작 변경(would_like 파생, 길이 격리)을 커밋 단위로
  분리한다.

## 구현 순서

### Step 1 — 프롬프트 빌더 신규 포맷 (방안 2·3)

`llm_generator.py`:

- `_candidate_block(videos)`: 객체 배열 → **배열-of-배열**로 변경. 각 후보를
  `[title(≤120), tags(≤8), channel(≤40), description(≤160)]` 순서로.
  truncation 한도는 현행 유지. video_id는 넣지 않는다.
- `build_action_log_prompt()`: 후보 블록 앞에 컬럼 순서와 "배열 위치 = 후보
  index" 명시. 출력형식 지시를 신규 출력 계약(`{"j":[[cp,wf],...]}`,
  "정확히 N개를 후보 순서대로", would_like 언급 제거)으로 교체.
- `ACTION_LOG_SYSTEM_HARNESS`: would_like 관련 문장 제거, 출력이 위치기반
  배열임을 반영.

### Step 2 — would_like 파생 규칙 (방안 6)

`pipeline.py`에 둔다(would_like가 세팅되는 `_build_user_drafts`와 근접):

- 모듈 상수 `WOULD_LIKE_CLICK_THRESHOLD = 0.7`,
  `WOULD_LIKE_WATCH_THRESHOLD = 0.6`.
- `derive_would_like(click_propensity, watch_fraction) -> bool` 헬퍼 추가.
- `RuleBasedActionLogGenerator.generate()`도 신규 출력 포맷(`{"j":[[cp,wf],...]}`)
  으로 맞추고 would_like는 출력에서 제거.

### Step 3 — 파싱 계약 변경 (방안 5·6)

`pipeline.py` `_build_user_drafts()`:

- `data["judgments"]` + `jmap`(video_id 매핑) 제거.
- `j = data["j"]` 읽고 `len(j) != len(candidates)`이면 `ValueError`
  발생시켜 `schema_fail` 격리로 흐르게 한다(패딩 금지).
- 후보 `i`별로 `entry = j[i]` → `click_propensity=_clamp01(entry[0])`,
  `watch_fraction=_clamp01(entry[1])`, `would_like=derive_would_like(...)`.
- entry가 길이 2 숫자 배열이 아니면 구조 오류 → `schema_fail`.
- 격리 error_type 분류(`invalid_json`/`schema_fail`)가 기존 호출부와
  일관되는지 확인(`_generate_drafts_isolated`의 예외 처리 경로).

### Step 4 — 버전 범프 (방안 공통)

`schema.py`:

- `PROMPT_VERSION = "action_log_ctr_v1"` → `"action_log_ctr_v2"`.
- 영향 확인: `daily.py:447/1010/1087`(manifest·체크포인트 게이트),
  `pipeline.py:715`(manifest). 코드 변경은 없고 상수 값만 전파됨을 확인.
- `daily.py:1087`의 prompt_version 불일치는 `ValueError` raise다. 이 예외를
  호출하는 상위 경로가 "구버전 shard 재생성"으로 흡수하는지 "런 실패"로
  전파하는지 확인하고, 필요 시 롤아웃 순서(구버전 산출물 정리)를 조정한다.

### Step 5 — 테스트 갱신

- `tests/test_action_logs_llm_generator.py`: 신규 프롬프트 문자열·
  RuleBased 출력 포맷·`derive_would_like` 단위 테스트. video_id가 프롬프트에
  없음, 출력이 위치배열임을 검증.
- `tests/test_action_logs_pipeline.py`: `_build_user_drafts` 인덱스 정렬,
  길이 불일치 → 격리, would_like 파생값 검증. 기존 video_id 매핑 기반
  테스트를 위치기반으로 교체.
- `tests/test_action_logs_daily.py:264`: 하드코딩된 `"action_log_ctr_v1"`을
  `"action_log_ctr_v2"`(또는 `PROMPT_VERSION` import)로 갱신.
- fixture 골든/샘플 응답이 있으면 신규 포맷으로 갱신.

### Step 6 — would_like 임계값 캘리브레이션

- 소량 실호출 샘플(또는 기존 생성물)로 현행 `would_like` true 비율을 측정하고,
  `T_CLICK`/`T_WATCH`를 근사하도록 조정. 결과를 spec의 "결정 필요" 항목에 확정
  기록.
- (선택) 실호출이 불가하면 RuleBased 분포 기준으로 기본값 유지하고, 첫 실배치
  격리·분포를 모니터링하는 것으로 대체.

## 검증 체크리스트

```bash
uv run python -m pytest tests/test_action_logs_llm_generator.py \
  tests/test_action_logs_pipeline.py tests/test_action_logs_daily.py -v
uv run python -m pytest -q            # 전체 회귀
```

- [ ] 신규 프롬프트에 video_id 미포함, 후보 블록이 배열-of-배열.
- [ ] 출력 `{"j":[[cp,wf],...]}` 파싱 정상, 위치 정렬 정확.
- [ ] 길이 불일치 응답 → `schema_fail` 격리 (오정렬 은폐 없음).
- [ ] would_like 파생값이 임계값 규칙과 일치, parquet에 정상 저장.
- [ ] like 이벤트 볼륨(발생률)이 현행 대비 허용 범위 내(캘리브레이션 결과).
- [ ] `PROMPT_VERSION` = v2가 manifest·체크포인트 게이트에 전파.
- [ ] parquet/warehouse 스키마·행 계약 회귀 없음.
- [ ] (측정) 입력·출력 토큰이 목표 근처로 감소.

## 롤아웃 주의

- `PROMPT_VERSION` 범프로 진행 중 체크포인트가 무효화된다. 진행 중 배치가 없는
  시점에 배포한다.
- 첫 실배치에서 `schema_fail` 격리 비율과 would_like true 비율을 모니터링하고,
  이상 시 임계값/프롬프트 문구를 조정한다.
- 롤백 경로: `PROMPT_VERSION`과 프롬프트/파싱을 v1으로 되돌리면 되며, 스키마·
  다운스트림은 불변이라 데이터 마이그레이션은 불필요.
