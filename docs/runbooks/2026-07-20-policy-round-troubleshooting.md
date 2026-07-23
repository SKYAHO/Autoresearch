# 정책 시뮬레이션 라운드 구현 트러블슈팅 기록

> 대상: 이슈 #195 (분할: #198, #199) / PR #200, #201, #203
> 기록일: 2026-07-20
> 관련 문서: `docs/specs/2026-07-20-policy-simulation-round.md`,
> `docs/archive/plans/2026-07-20-policy-simulation-round.md`

3개 PR로 분할 구현하며 만난 문제와 해결을 유형별로 기록한다. 같은 유형의
작업(스키마 확장, 추출 리팩터링, 분할 PR)에서 재발 가능성이 높은 항목은
"교훈"에 일반화해 두었다.

## 1. 계획-현실 충돌

### 1.1 정확 동등(`==`) 계약 테스트 vs additive 스키마 확장

- **위치**: PR #201 (Task 2, EventLog 확장)
- **증상**: EventLog에 optional 필드 4개(`policy`/`ctr_score`/`is_exploration`/
  `policy_version`)를 추가하자 기존 `tests/test_action_logs_pipeline.py`의
  warehouse JSONL row 검증이 실패. 해당 테스트는 row 키 집합을 **정확
  동등(`==`)** 으로 단언하고 있었고, 구현 계획은 이를 간과한 채 "기존 테스트
  무수정 통과"를 전제로 했다.
- **해결**: 구현자가 임의 수정 대신 작업을 중단하고 에스컬레이션. spec이
  warehouse 직렬화 확장을 명시적으로 승인했으므로 "의도된 계약 변경의
  문서화 갱신"으로 판정하고, 해당 단언의 기대 키 집합에 신규 4개 키만
  추가했다(그 외 기존 테스트는 전부 무수정).
- **교훈**: additive 확장은 부분집합(`<=`) 단언과는 공존하지만 정확 동등
  단언과는 **반드시** 충돌한다. 스키마 확장을 계획할 때 대상 계약의 `==`
  단언 테스트를 사전에 grep해서 계획에 갱신을 명시할 것.

### 1.2 빈 DataFrame의 DuckDB dtype 추론

- **위치**: PR #203 (Task 6, 배치 진입점)
- **증상**: 테스트 픽스처 `_empty_events()`가 dtype 없는 빈 DataFrame을
  반환했는데, DuckDB가 빈 컬럼을 INTEGER로 추론해
  `compute_point_in_time_user_features`의 `past.user_id = q.user_id`
  문자열 비교가 `ConversionException`으로 실패 (테스트 3건 전멸).
- **해결**: 프로덕션 코드는 그대로 두고 픽스처에 dtype을 명시
  (`user_id`/`video_id`/`timestamp` → `string`, 카운트류 → `Int64`).
- **잔여 리스크(후속 이슈 후보)**: 실제 cold-start(이력이 정말 비어 있는
  유저 집단)에서 프로그램적으로 빈 events를 넘기면 동일 크래시가 재현된다.
  `compute_point_in_time_user_features`(#198 산출물) 소유의 견고성 갭.
- **교훈**: 빈 DataFrame을 DuckDB에 register하는 모든 경로(특히 테스트
  픽스처)는 `.astype()`으로 dtype을 명시할 것.

### 1.3 추출 리팩터링의 dead code 연쇄

- **위치**: PR #200 (Task 1, 피처 조립 추출)
- **증상**: 코드 이동 후 원 파일(`build_training_dataset.py`)에 잔재 발생 —
  미사용 `import json`, 유일 소비자가 사라진 `con.register("event_log", ...)`,
  재배선 후 미사용이 된 재수입 import 2건. CI Ruff(F401)에서 실패할 상태.
- **해결**: 픽스 커밋으로 일괄 정리. 지시 범위(3건) 밖의 같은 클래스 잔재
  2건을 픽스 담당이 추가 발견해 함께 제거했고(스코프 초과를 명시 보고),
  재리뷰에서 정당 판정.
- **부수 발견**: 계획 템플릿에 없던 `KEYWORD_TO_CATEGORY` 검증 assert는
  dict와 함께 이동하지 않으면 NameError가 나므로 동반 이동이 필수였다.
- **교훈**: "이동" 리팩터링 계획에는 원 파일의 import·register 잔재 정리와
  이동 심볼을 참조하는 모듈 수준 문장(assert 등)의 동반 이동을 명시적
  스텝으로 넣을 것.

## 2. 프로세스·인프라

### 2.1 plan 문서가 main에 없어 분할 브랜치가 참조 불가

- **증상**: 구현을 3개 이슈로 분할했는데, 작업 지시서(plan)가 아직 #195
  브랜치에만 있어 main 기준으로 파는 새 브랜치들이 참조할 수 없었다.
- **해결**: 문서 전용 PR(#197)을 먼저 머지한 뒤 분할 브랜치를 생성하는
  순서로 재배열.
- **교훈**: 분할 구현에서는 spec/plan 문서 머지가 항상 첫 번째 PR이다.

### 2.2 선행 PR 미머지 상태에서 본체 착수 (stacked branch)

- **증상**: Task 5–8은 #198(피처 조립)·#199(action_logs 확장) 코드에
  의존하는데 두 PR이 리뷰 대기 상태였다.
- **해결**: main + 두 feature 브랜치를 octopus merge한 stacked 브랜치
  (`feat/195-policy-simulation-core`)로 진행. PR #203 본문에 "선행 PR
  머지 후 diff가 자기 커밋만 남는다"를 명시해 리뷰 혼선을 방지.

### 2.3 서브에이전트 재개 실패와 미커밋 인계

- **증상**: Task 2 구현자가 결정 대기(NEEDS_CONTEXT) 상태로 종료된 뒤
  재개가 불가능했다(트랜스크립트 유실). 작업은 워킹 트리에 미커밋으로 잔존.
- **해결**: 새 에이전트를 파견해 미커밋 변경을 요구사항 문서와 대조
  검증한 뒤 이어받아 마무리. 구현자가 "미커밋 보존 + 보고서 파일 기록"
  관례를 지킨 덕에 손실 없이 인계됐다.
- **교훈**: 서브에이전트 보고서는 파일로 남기고, 블로킹 시 워킹 트리를
  커밋하지 않은 채 보존하는 관례가 인계 안전망이 된다.

### 2.4 이전 세션의 SDD 잔재 (예방 성공)

- 시작 시 `.superpowers/sdd/`에 이전 브랜치(feat/160)의 brief/report/diff
  20여 개가 잔존해 있었다. 과거 세션에서 실제 사고(오염된 리뷰)를 겪었던
  항목이라 시작 전에 전부 삭제하고 원장을 새로 작성 — 이번에는 사고 없음.

## 3. 외부 리뷰(claude 봇·archmap)에서 드러난 것

### 3.1 품질 잡의 구 파티션 소급 스캔 실패 — 유일한 실질 발견

- **내용**: parquet 스키마가 8→12 필드로 확장되면서, 구 스키마로 생성된
  기존 final parquet 파티션을 품질 잡(`action_log_quality`)이 **소급
  스캔하면** `missing_columns` 검증이 실패한다(코드로 확인:
  `summarize_final_schema` → `validate_summary` 에러 추가). 일상
  경로(당일 생성 파티션 검사)에서는 발생하지 않는다.
- **상태(미결)**: 소급 스캔이 필요해지는 시점에 (a) 해당 파티션 재생성
  또는 (b) nullable 신규 컬럼에 한해 missing 검사 완화 중 선택 필요.
  Airflow 스케줄 소유 영역이므로 별도 논의 대상 (#201 리뷰 스레드 참조).
- **참고**: 롤백(12필드 파티션 + 8필드 코드)은 양방향 모두
  `schema.equals` 실패 → 재생성 강제이므로 silent 오염 경로는 없다.

### 3.2 archmap "파괴적" 판정 — 실질 무영향

- **내용**: `KEYWORD_TO_CATEGORY`·`derive_preferred_category`의 모듈 표면
  이탈(이동)을 파괴적으로 판정. 저장소 전체 grep으로 외부 소비자 부재를
  확인해 실질 무영향으로 대응 완료. 공식 import 경로는 이제
  `src.features.assembly`.

## 4. 남은 액션

| 항목 | 소유 | 출처 |
| --- | --- | --- |
| cold-start(빈 events) dtype 견고성 후속 이슈 발행 | #198 산출물 (`src/features/assembly.py`) | §1.2 |
| 품질 잡 구 파티션 소급 스캔 방침 결정 (재생성 vs 검사 완화) | Airflow 운영 | §3.1 |
