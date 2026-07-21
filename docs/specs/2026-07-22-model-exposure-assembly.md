# 모델 노출 조립 — user_recommendations 기반 70/20/10 노출 provider

- 날짜: 2026-07-22
- 이슈: #221
- 선행: #216(일일 추천 배치, PR #217) · #219(운영 블로커 수정, PR #223)
- 후속: #222(일일 폐루프 완성 — CLI 전환·cutover)
- 관련: docs/specs/2026-07-20-policy-simulation-round.md (정책 메타데이터·provider seam),
  docs/specs/2026-07-21-daily-recommendations-batch.md (입력 테이블 계약)

## 배경

멘토링(2026-07-21) 합의 아키텍처에서 일일 action-log의 노출 24개는
**모델 예측 70% + 트렌딩 20% + 랜덤 10%**로 구성되고, 각 노출에는 어느 정책으로
나갔는지 태그가 남아야 이후 온라인 매트릭 비교(AB)가 가능하다.

현재 일일 action-log 생성(`autoresearch/action_logs/candidate.py`)의 70%
슬라이스는 키워드 substring 겹침 휴리스틱이다. #216이 champion 모델의 유저별
전체 순위를 `user_recommendations` 파티션 테이블로 매일 적재하므로, 이 순위를
70% 슬라이스의 소스로 교체하면 폐루프의 전반부(모델 → 노출)가 연결된다.

## 목표

1. `user_recommendations`의 dt 파티션을 읽어 유저별 모델 순위를 제공하는
   BQ 리더 구현
2. 모델 순위 상위 + 트렌딩 + 랜덤으로 24개 노출을 조립하는
   `CandidateProvider` 호환 provider 구현 (기존 seam:
   `autoresearch/action_logs/pipeline.py`의 `candidate_provider` 주입)
3. 노출별 정책 태그(exposure_source)와 모델 계보(model_run_id)가 event log
   레코드에 남는 계약 정의

## 비범위 (후속 이슈 소관)

- 일일 action-log CLI가 이 provider를 기본 사용하도록 전환하는 배선,
  휴리스틱 fallback 플래그, cutover 절차 — **#222**
- LLM 판정·클릭 정규화 로직 변경 — 없음 (기존 경로 그대로)
- Airflow DAG 편성 — `Autoresearch-airflow` 소관
- 학습 나이 계산 정합 — #224

## 입력 계약

### user_recommendations (읽기)

`docs/specs/2026-07-21-daily-recommendations-batch.md`의 출력 테이블을 그대로
소비한다. 소비 컬럼: `dt`(파티션 필터), `user_id`, `video_id`, `rank`,
`ctr_score`, `model_run_id`, `model_version`.

- 유저별 `rank` 오름차순이 모델 선호 순위다. `ctr_score`는 보정된 확률이
  아닌 순위용 상대 점수임을 그대로 기록에 전파한다.
- **공유 계약 주의**: #220(서빙 Feature Build)의 온라인 경로도 같은 스키마로
  이력을 기록할 예정이다. 이 spec의 리더는 위 컬럼 집합만 가정하고, 추가
  컬럼을 무시한다(전방 호환).

### 트렌딩 영상 (읽기)

노출 후보 dict는 LLM 판정에 `title`/`description`/`tags` 텍스트가 필요하므로
`user_recommendations`만으로는 부족하다. 기존 일일 action-log 배치가 로드하는
트렌딩 파티션의 video dict 목록을 함께 받아 `video_id`로 조인한다.

### dt 정합 (하나의 dt 원칙)

`recommendations.dt == 트렌딩 videos.dt == 노출 생성 대상 dt`. provider는
dt를 명시 인자로 받으며, 해당 dt의 `user_recommendations` 파티션이 비어
있으면 **fail-fast**(RuntimeError)한다 — 조용히 휴리스틱으로 대체하지 않는다
(대체 정책과 fallback 플래그는 #222에서 정의).

## 노출 조립 규칙

### 슬롯 산식 (기존 build_candidates 산식 재사용)

```
n_total    = min(candidates_per_user, len(videos))        # 기본 24
n_popular  = round(n_total × popular_ratio / ratio_sum)    # 0.2 → 5
n_explore  = round(n_total × exploration_ratio / ratio_sum)# 0.1 → 2
n_model    = n_total - n_popular - n_explore               # 나머지 → 17
```

기본값(24, 0.7/0.2/0.1)에서 모델 17 · 트렌딩 5 · 랜덤 2 — 멘토링에서 합의된
"모델 예측 대상 ~17개"와 일치한다.

### 슬롯 채움 (결정론·중복 제거)

1. **model**: 해당 유저의 `rank` 오름차순 상위에서 `n_model`개.
2. **trending**: `view_count` 내림차순(동점 video_id) 상위에서 `n_popular`개 —
   기존 popular 슬롯 로직과 동일. model 슬롯과 겹치면 다음 인기 영상으로.
3. **random**: 남은 pool에서 `user_rng`로 `n_explore`개.
4. 전 슬롯 공통 seen-set으로 video_id 중복을 차단한다.
5. 최종 목록은 `user_rng`로 셔플한다(LLM 위치 편향 방지, 기존 동작 유지).
   `user_rng`는 pipeline이 주입하는 `random.Random(f"{seed}:{user_id}")`
   그대로 — 동일 seed 재실행 시 동일 노출.

### 부족분 채움 (멘토링에서 미고려로 확인된 케이스)

- **유저의 모델 순위 행이 n_model보다 적을 때** (예: 추천 배치에서 해당 유저
  격리, pool 축소): 부족분은 trending → random 순서로 이어 채워 `n_total`을
  유지한다. 채움분의 태그는 실제 소스(trending/random)를 따른다 — model로
  위장하지 않는다.
- **유저의 추천이 아예 없을 때**: 노출은 trending + random만으로 구성된다.
  해당 유저의 로그에는 `exposure_source="model"` 행이 없으므로 정책 비교
  집계에서 자연스럽게 구분된다. 키워드 휴리스틱으로 대체하지 않는다
  (정책 오염 방지 — 휴리스틱 경로는 #222의 명시적 fallback 플래그로만).
- **video_id 조인 실패** (추천에는 있으나 트렌딩 dict에 없음): 해당 행은
  건너뛰고 다음 순위로 채운다. 건수를 로그로 남긴다.

## 정책 태그 계약

### EventLog 스키마 확장 (additive)

`autoresearch/action_logs/schema.py`의 `EventLog`에 optional 필드 1개를
추가한다. 기존 historical 로그와 하위 호환(전부 None 허용).

| 필드 | 타입 | 의미 |
| --- | --- | --- |
| `exposure_source` | `Literal["model", "trending", "random"] \| None` | 이 노출이 어느 슬롯으로 나갔는지 |

`to_warehouse_row()`와 warehouse 적재 스키마에 nullable 컬럼으로 추가된다.
품질 계약(action_log_quality)에는 "값이 있으면 세 값 중 하나" 검증만
추가한다(soft — 과거 로그는 None).

### 기존 정책 메타데이터 필드 매핑 (#195 재사용)

| 필드 | 값 |
| --- | --- |
| `policy` | `"model"` (이 provider가 만든 라운드 전체) |
| `policy_version` | `user_recommendations.model_run_id` (champion 계보) |
| `rank` | model 슬롯: 유저별 모델 순위. trending/random 슬롯: 노출 목록 내 위치 |
| `ctr_score` | model 슬롯: `user_recommendations.ctr_score`. 그 외: None |
| `is_exploration` | `exposure_source == "random"` (기존 필드와 의미 일치 유지) |

`model_version`은 레코드에 별도 필드로 남기지 않는다 — `policy_version`
(run_id)으로 MLflow에서 역추적 가능하고, 필드 중복을 피한다.

### LLM 비노출 원칙

정책 태그·점수는 LLM 프롬프트에 노출되지 않는다.
`build_action_log_prompt`는 title/description 텍스트만 소비하고, 태그는
provider가 유지하는 노출 메타데이터를 draft → EventLog 조립 시
`(user_id, video_id)`로 조인해 붙인다 — simulate_policy_round가 Exposure
메타데이터를 조인하는 기존 패턴과 동일하다.

## 배치 구성 요소와 소유

| 구성 요소 | 위치 | 이유 |
| --- | --- | --- |
| BQ 리더 `load_user_rankings(client, table_id, dt)` | `src/pipeline/` (신규 모듈) | BigQuery 의존은 src/pipeline 소관 — `autoresearch/action_logs/`는 순수 유지 |
| 노출 조립 `build_model_exposures(...)` | 같은 신규 모듈 | 순수 함수 — BQ 없이 단위 테스트 |
| provider 클로저 (조립 결과를 `CandidateProvider` 시그니처로) | 같은 신규 모듈 | 기존 seam `candidate_provider(virtual_user, user_rng)`에 그대로 주입 |
| `EventLog.exposure_source` 필드 | `autoresearch/action_logs/schema.py` | 스키마 단일 출처 |

리더는 파티션을 **1회** 조회해 `user_id → 순위 목록` 맵을 만든 뒤 유저별
호출에서 재사용한다(유저 수 × 반복 조회 금지 — #216 리뷰의 반복 I/O 지적과
동일 원칙).

## 에러 처리

- **fail-fast**: 대상 dt의 `user_recommendations` 파티션 부재/0행, 트렌딩
  videos 0건 — provider 생성 시점에 RuntimeError.
- **유저 단위 격리**: 특정 유저의 조립 실패(조인 전무 등)는 해당 유저 노출을
  빈 목록으로 반환 — pipeline의 기존 "빈 후보 skip" 경로를 따른다.
- **부분 데이터 허용**: 모델 순위 부족·조인 실패 건너뜀은 오류가 아니라
  채움 규칙으로 처리하고 건수를 로깅한다.

## 테스트 계획

`tests/test_model_exposure_provider.py` (신규, fake BQ client):

1. 슬롯 수: 24개 기본에서 model 17 · trending 5 · random 2
2. model 슬롯이 rank 오름차순을 따르고 ctr_score·policy_version이 전파됨
3. 중복 제거: 모델 상위와 트렌딩 겹침 시 다음 인기 영상으로 채움
4. 부족분: 모델 순위 5개뿐인 유저 → trending/random으로 24개 유지, 태그가
   실제 소스를 따름
5. 추천 없는 유저 → model 슬롯 0, trending+random 구성
6. 결정론: 동일 seed 재실행 시 동일 노출·순서
7. dt 파티션 부재 → RuntimeError
8. `exposure_source` 스키마: 값 검증·to_warehouse_row 왕복·기존 로그(None)
   하위 호환

## 미해결 / 후속 (#222로 이월)

- 일일 CLI 배선과 `--exposure-source model|heuristic` 선택 플래그
- `user_recommendations` 파티션 지연 시 대기/skip/fallback 운영 정책
- exposure_source별 CTR 집계 리포트(온라인 매트릭 비교)
