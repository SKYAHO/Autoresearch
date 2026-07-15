# Plan: Action Log 프롬프트 토큰 절감 구현

- Status: Complete
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
  index" 명시. 출력형식 지시를 신규 출력 계약(`{"j":[[index,cp,wf],...]}`,
  "index 0~N-1 각 1회", would_like 언급 제거)으로 교체. (index는 리뷰 반영으로
  추가 — 재정렬 방어.)
- `ACTION_LOG_SYSTEM_HARNESS`: would_like 관련 문장 제거, 출력이 인덱스
  객체(`{"j":[[index,cp,wf],...]}`)임을 반영.

### Step 2 — would_like 파생 규칙 (방안 6)

`pipeline.py`에 둔다(would_like가 세팅되는 `_build_user_drafts`와 근접):

- 모듈 상수 `WOULD_LIKE_CLICK_THRESHOLD = 0.7`,
  `WOULD_LIKE_WATCH_THRESHOLD = 0.6`.
- `derive_would_like(click_propensity, watch_fraction) -> bool` 헬퍼 추가.
- `RuleBasedActionLogGenerator.generate()`도 신규 출력 포맷
  (`{"j":[[i,cp,wf],...]}`, `enumerate`)으로 맞추고 would_like는 출력에서 제거.

### Step 3 — 파싱 계약 변경 (방안 5·6)

`pipeline.py` `_build_user_drafts()`:

- `data["judgments"]` + `jmap`(video_id 매핑) 제거.
- `j = data["j"]` 읽고 각 원소 `[index, cp, wf]`를 `index`로 `by_index`에 모은다.
- **index 무결성 검증** → 위반 시 `ValueError`(→`schema_fail`):
  개수 불일치(`len(j)!=n`), 원소 길이≠3, index 비정수(bool 배제·정수값 float 허용),
  범위 이탈(`index∉[0,n)`), 중복 index. 세 조건(len==n·범위·중복없음) 성립 시
  누락도 배제되어 index 집합이 정확히 `0..n-1`.
- 후보 `i`: `cp,wf = by_index[i]` → `_clamp01` 적용, `would_like=derive_would_like`.
- 격리 error_type 분류(`invalid_json`/`schema_fail`)가 기존 호출부와
  일관되는지 확인(`_generate_drafts_isolated`의 예외 처리 경로).

### Step 4 — 버전 범프 (방안 공통)

`schema.py`:

- `PROMPT_VERSION = "action_log_ctr_v1"` → `"action_log_ctr_v2"`.
- 영향 확인: `daily.py:447/1010/1087`(manifest·체크포인트 게이트),
  `pipeline.py:715`(manifest). 코드 변경은 없고 상수 값만 전파됨을 확인.
- **확인 완료** (리뷰 반영): `prompt_version`은 `_fingerprint_payload`(daily.py:447)
  에 포함되고 체크포인트 namespace가 `fingerprint={config_fingerprint}`(daily.py:529)로
  격리된다. 따라서 (a) 생성/재개: v2는 새 fingerprint→새 namespace→v1 파트 미검출로
  **재생성**(구 v1 체크포인트는 고아 방치, 손상 아님), (b) 병합: `_load_shard_manifests`가
  v1 매니페스트를 `ValueError`로 **거부**(v1/v2 혼재 병합 불가), (c) 롤백 v2→v1:
  fingerprint 복귀로 v1 namespace 재활성, v2 산출물은 고아 방치·병합 거부 → **데이터
  손상 없음**. 실질 우려는 고아 v1 작업의 재계산 낭비뿐 → 진행 중 배치 없을 때 배포.

### Step 5 — 테스트 갱신

- `tests/test_action_logs_llm_generator.py`: 신규 프롬프트 문자열·
  RuleBased 출력 포맷·`derive_would_like` 단위 테스트. video_id가 프롬프트에
  없음, 출력이 인덱스 삼중배열임을 검증.
- `tests/test_action_logs_pipeline.py`: `_build_user_drafts` index 재결합,
  **재정렬 응답 정상 매핑**, 개수불일치·중복·범위이탈·원소길이 오류 → 격리,
  would_like 파생값 검증. 기존 video_id 매핑 기반 테스트를 index 기반으로 교체.
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
- [ ] 출력 `{"j":[[index,cp,wf],...]}` 파싱 정상, **재정렬 응답도 index로 정확 매핑**.
- [ ] 개수불일치·중복·범위이탈·원소길이 오류 → `schema_fail` 격리 (오정렬 은폐 없음).
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

## Step 7 — PR #119 Claude 리뷰 후속 수정

### 구현 결정

- `_ActionLogCallResult`가 최종 draft, 오류 유형, 실제 예외,
  `request_elapsed_ms`, `parse_elapsed_ms`를 함께 반환하도록 결과 계약을 확장한다.
- 최초 요청부터 선택적 schema retry와 재파싱까지 수행하는 worker helper를 두고,
  coordinator의 동기 `schema_retry()` 호출과 보정용 elapsed subtraction을 제거한다.
- 각 request 호출 직전·직후와 각 parse 호출 직전·직후를 별도로 측정해 두 지표가
  겹치지 않게 누적한다.
- 재시도 API 오류에서는 최종 예외를 버리지 않고 `api_error`로 반환한다. 최초
  응답 파싱이 이미 수행됐다면 그 시간은 parse 지표에 보존한다.
- worker future의 예상하지 못한 내부 예외는 coordinator에서 `api_error`로
  바꾸지 않고 전파해 내부 버그를 외부 장애로 위장하지 않는다.
- coordinator는 worker 결과를 `QuarantineRecord` 또는 checkpoint 성공으로
  변환하고, 기존 원본 work 순서 조립을 유지한다.

### 추가 테스트

- [x] block된 schema retry와 별개로 완료된 worker 슬롯에 다음 work가 투입된다.
- [x] schema retry API 오류가 최종 예외·`api_error`·최초 raw 응답을 보존한다.
- [x] 제어된 clock에서 request 합계는 두 네트워크 호출만, parse 합계는 두 검증
  호출만 포함한다.
- [x] 기존 invalid JSON/schema 복구, 최종 검증 실패 quarantine 테스트가 유지된다.
- [x] 예상하지 못한 worker 내부 예외가 `api_error`로 격리되지 않고 전파된다.
- [x] 대상 action-log 테스트와 전체 pytest, `git diff --check`가 통과한다.

### 검증 결과

- `tests/test_action_logs_pipeline.py`: 35 passed.
- action-log 관련 4개 테스트 모듈: 78 passed.
- 전체 테스트: 249 passed, 2 skipped. skip은 기존 Docker 사용 가능 여부 검사다.
- Ruff 대상 파일 검사와 `git diff --check` 통과.
