# Model Feature Contract SSOT와 21개 Feature 전환 (#251)

- **상태**: Implemented
- **날짜**: 2026-07-22
- **이슈**: [#251](https://github.com/SKYAHO/Autoresearch/issues/251)
- **관련 문서**: `docs/guides/training-dataset.md`,
  `docs/guides/data-warehouse.md`,
  `docs/specs/2026-07-21-training-dataset-16-to-21-column-roadmap.md`,
  `docs/specs/2026-07-16-reranking-serving-api.md`

## 목적

CTR 모델이 사용하는 feature 이름, 순서, categorical 분류를 하나의 Python
계약으로 정의하고 학습, 평가, Inference Server, 일일 추천 batch가 같은 계약을
소비하게 한다. 기존 분산 feature 목록을 이미 학습 데이터와 FeatureStore가
제공하는 canonical 21개 model input으로 통합한다.

이 변경은 두 문제를 함께 해결한다.

1. 학습 설정과 serving 코드에 feature 목록이 중복되어 한쪽만 갱신될 수 있다.
2. `training_dataset`과 FeatureStore는 신규 6개 feature를 제공하지만 실제 모델과
   serving은 아직 기존 15개만 사용한다.

## 현재 상태

| 계층 | 현재 계약 | 결과 |
| --- | --- | --- |
| `src/pipeline/build_training_dataset.py` | 21개 model input + `clicked` | 실제 CSV는 22컬럼 |
| `feature_repo/feature_definitions.py` | 신규 6개를 포함한 FeatureView | online 조회 가능한 스키마는 준비됨 |
| `src/pipeline/config.yaml` | model input 목록을 소유하지 않음 | 학습은 canonical contract를 직접 소비 |
| `src/serving/online_features.py` | canonical contract 기반 read refs와 21개 builder output | serving은 별도 feature list를 정의하지 않음 |
| `src/pipeline/simulate_policy_round.py` | canonical 21개 frame 조립 | 일일 추천도 같은 contract를 소비 |
| `src/pipeline/evaluate.py`, `src/serving/model_loader.py` | canonical 입력과 artifact strict 검증 | 불일치 artifact는 실행 경계에서 거부 |

새 train artifact, evaluator, serving builder, simulation/daily, loader는 함께
cutover해야 한다. 15개 artifact를 새 경로에서 계속 지원하거나 누락 컬럼을
padding하는 방식은 사용하지 않으며, 계약 불일치는 startup/healthcheck 또는
batch 시작 경계에서 명시적으로 거부한다.

## 범위

### 포함

- canonical 21개 model feature 이름과 순서
- categorical feature 목록
- 학습, model artifact, online serving, 일일 추천/simulation의 계약 공유
- 신규 6개 online read와 typed cold-start default
- 계약 불일치 fail-fast와 안전한 promote 순서
- 21개 feature + `clicked` label인 22컬럼 training dataset 명시

### 제외

- FeatureStore source table 생성 SQL 변경
- Feast entity 또는 FeatureView 재설계
- `topic_similarity` embedding 생성 방식 변경
- 모델 hyperparameter 및 평가 기준 변경
- Airflow schedule, retry, image rollout 구현
- 임의 feature subset을 production champion으로 serving하는 기능

## 설계 결정

### 1. 공통 Python 계약이 SSOT다

`src/features/model_contract.py`를 추가하고 다음 불변 값을 소유하게 한다.

```python
MODEL_FEATURE_COLUMNS: Final[tuple[str, ...]]
CATEGORICAL_FEATURE_COLUMNS: Final[tuple[str, ...]]
```

학습, Inference Server, 일일 추천/simulation은 이 값을 직접 import한다. 모델
artifact의 `feature_columns.pkl`과 `categorical_columns.pkl`은 SSOT가 아니라
학습 결과를 재현하기 위한 스냅샷이다. artifact를 읽는 경계에서는 스냅샷이 공통
계약과 순서까지 정확히 같은지 검증한다.

`src/pipeline/config.yaml`의 feature 목록은 제거한다. 내부 production 학습
파이프라인이 하나의 고정 serving 계약을 사용하므로 같은 목록을 설정에 다시
적어둘 이유가 없다. 모델 hyperparameter와 데이터 경로는 계속 YAML이 소유한다.

### 2. canonical model input은 다음 21개다

순서는 `docs/guides/training-dataset.md`의 Model Input Columns를 따른다. 모델
학습 DataFrame과 추론 DataFrame은 반드시 이 순서로 생성한다.

| 순서 | Feature | 종류 | 온라인 출처 또는 계산 | cold-start default |
| ---: | --- | --- | --- | --- |
| 1 | `age_group` | categorical | `UserStaticView` | `unknown` |
| 2 | `occupation` | categorical | `UserStaticView` | `unknown` |
| 3 | `watch_time_band` | categorical | `UserStaticView` | `unknown` |
| 4 | `recent_click_count_7d` | integer | `UserDynamicView` | `0` |
| 5 | `recent_view_count_7d` | integer | `UserDynamicView` | `0` |
| 6 | `recent_watch_time_7d` | integer | `UserDynamicView` | `0` |
| 7 | `recent_like_count_7d` | integer | `UserDynamicView` | `0` |
| 8 | `historical_category_affinity` | categorical | `UserDynamicView` | `unknown` |
| 9 | `total_event_count_7d` | integer | `UserDynamicView` | `0` |
| 10 | `category_id` | categorical | `VideoFeatureView` | `unknown` |
| 11 | `duration_sec` | integer | `VideoFeatureView` | `0` |
| 12 | `view_count` | integer | `VideoFeatureView` | `0` |
| 13 | `like_ratio` | float | `VideoFeatureView` | `0.0` |
| 14 | `comment_ratio` | float | `VideoFeatureView` | `0.0` |
| 15 | `days_since_upload` | integer | `VideoFeatureView` | `0` |
| 16 | `channel_subscriber_count` | integer | `VideoFeatureView` | `0` |
| 17 | `channel_view_count` | integer | `VideoFeatureView` | `0` |
| 18 | `channel_video_count` | integer | `VideoFeatureView` | `0` |
| 19 | `topic_similarity` | float | `UserCategorySimilarityView` | `0.0` |
| 20 | `preferred_category_match` | binary integer | `preferred_category`와 `category_id` 비교 | `0` |
| 21 | `historical_category_match` | binary integer | `historical_category_affinity`와 `category_id` 비교 | `0` |

categorical feature는 다음 5개로 고정한다.

```text
age_group
occupation
watch_time_band
historical_category_affinity
category_id
```

`clicked`는 label이며 model feature 계약에 포함하지 않는다. 따라서 최종
`training_dataset.csv`의 22번째 physical column은 `clicked`이고, 전체는 21개
input feature + label = 22컬럼이다.
dataset의 물리적 컬럼 순서와 무관하게 학습 입력은
`MODEL_FEATURE_COLUMNS`로 명시적으로 선택한다.

### 3. online read 계약은 serving 계층이 소유한다

model feature 목록과 Feast read refs는 같은 개념이 아니다.
`preferred_category`처럼 모델에 직접 들어가지 않고 파생 feature 계산에만 쓰는
조회 컬럼이 있기 때문이다. 따라서 Feast ref 목록은
`src/serving/online_features.py`에 유지하되 공통 model contract를 만족해야 한다.

첫 번째 `(user_id, video_id)` batch read에 다음 신규 ref를 추가한다.

```text
UserStaticView:watch_time_band
UserDynamicView:recent_view_count_7d
UserDynamicView:total_event_count_7d
VideoFeatureView:channel_subscriber_count
VideoFeatureView:channel_view_count
VideoFeatureView:channel_video_count
```

`topic_similarity`의 두 번째 `(user_id, category_id)` batch read와
`preferred_category_match`/`historical_category_match` 파생 방식은 유지한다.
`ServingFeatureBuilder`가 반환하는 각 `CandidateVideo.features`는 canonical
21개를 모두 포함해야 한다.

### 4. 학습, 평가와 batch scoring도 같은 계약을 사용한다

`src/pipeline/train.py`와 `src/pipeline/evaluate.py`는 YAML에서 feature 목록을
읽지 않고 공통 계약으로 학습·평가 입력을 선택한다. 학습 후 저장하는 두
artifact도 공통 계약에서 생성한다.

`src/pipeline/simulate_policy_round.py`의 pool frame에는 다음 값을 추가한다.

- user offline: `watch_time_band`
- point-in-time user dynamic: `recent_view_count_7d`, `total_event_count_7d`
- video: 기존 `compute_video_features()`가 생성하는 `channel_*` 3개

`daily_recommendations.py`는 이 frame과 model artifact를 그대로 소비하므로 별도
feature 계산을 중복 구현하지 않는다. Candidate 변환 직전에 canonical 21개가
모두 있는지 검증하고, 누락 시 사용자 단위 skip으로 축소하지 않고 batch 계약
오류로 전체 실행을 실패시킨다. 모델 계약 오류는 데이터 한 사용자의 오류가
아니기 때문이다.

### 5. 계약 오류는 경계에서 fail-fast한다

다음 비교는 이름뿐 아니라 순서까지 정확히 같아야 한다.

```text
model artifact feature columns == MODEL_FEATURE_COLUMNS
model artifact categorical columns == CATEGORICAL_FEATURE_COLUMNS
training input columns == MODEL_FEATURE_COLUMNS
serving candidate columns ⊇ MODEL_FEATURE_COLUMNS
batch candidate columns ⊇ MODEL_FEATURE_COLUMNS
```

Inference Server는 기존 동작대로 불일치 시 `/healthcheck`를 503으로 만들고
`/rerank`을 처리하지 않는다. 학습과 일일 추천 batch는 실행 초기에 계약 오류로
실패한다. 누락 feature를 임의의 `NaN`으로 추가하거나 artifact가 요구하는 임의
목록을 동적으로 허용하지 않는다.

## 검토한 대안

### 대안 A: `config.yaml`을 SSOT로 사용

학습에는 자연스럽지만 serving image가 학습 설정 파일의 구조와 배포 경로에
의존한다. Python 타입과 import 경계에서 계약을 확인할 수도 없어 채택하지 않는다.

### 대안 B: model artifact를 SSOT로 사용

여러 feature subset 실험에는 유연하지만 artifact만으로는 각 feature의 Feast
출처, 타입, default, 파생 방식을 결정할 수 없다. 잘못 promote된 artifact가
serving 동작을 정의하게 되므로 채택하지 않는다.

### 대안 C: 15개와 21개를 영구적으로 동시 지원

배포 순서는 단순해지지만 계약 분기와 테스트가 계속 남고, 어떤 feature set이
production 기준인지 다시 모호해진다. 일시적인 migration 코드도 두지 않고
운영 cutover 순서로 해결한다.

## 전환과 rollout

train artifact, evaluator, serving builder, simulation/daily scoring, loader가
서로 다른 feature 계약을 사용한 상태로 실행되면 healthcheck 또는 batch가
실패하므로 이 다섯 경계를 독립적으로 cutover하지 않는다.

1. FeatureStore source table에 신규 6개 컬럼이 존재하고 Redis materialize가
   완료됐는지 검증한다.
2. 이 변경의 테스트와 serving/batch image build를 완료하되 production에는 아직
   rollout하지 않는다.
3. 새 image로 21개 모델을 학습하고 evaluator의 offline 평가를 승인한 뒤
   registry에 등록한다. 이 단계에서는
   `@champion` alias를 변경하지 않는다.
4. 일일 추천 schedule을 일시 중지하거나 새 image 사용이 보장된 실행 시각을
   선택한다.
5. 검증된 21개 모델과 serving builder, simulation/daily image, strict loader를
   같은 cutover에 올리고 `@champion` alias를 promote한다.
6. `/healthcheck`, 실제 `/rerank`, 일일 추천 batch를 확인한 뒤 schedule을
   재개한다. 이전 15개 image/pod는 별도 rollback 경계일 뿐 새 artifact와
   호환되는 경로가 아니다.

기존 pod가 alias 변경 후 모델을 자동 reload하는 배포 환경이라면 6번의 무중단
전제가 성립하지 않는다. 이 경우 promote와 rollout 동안 maintenance window를
사용하거나 배포 계층에서 candidate model version을 명시적으로 pin해야 한다.
model reload 여부 확인은 production cutover의 필수 사전 조건이다.

## 검증 계약

### 단위/계약 테스트

- canonical feature가 정확히 21개이며 중복이 없고 문서화된 순서와 같다.
- categorical 5개가 모두 canonical feature의 부분집합이다.
- training config에 별도 feature 목록이 남아 있지 않다.
- `ServingFeatureBuilder`가 신규 6개 ref를 조회하고 요청 영상 순서를 보존한다.
- 신규 categorical 결측은 `unknown`, 신규 numeric 결측은 `0`으로 조립된다.
- 잘못된 타입, 누락 컬럼, entity key 불일치는 기존처럼 실패한다.
- simulation pool frame이 canonical 21개를 모두 제공한다.
- 15개 artifact는 21개 공통 계약과 불일치하여 startup/batch에서 실패한다.
- 순서만 다른 21개 artifact도 계약 오류로 실패한다.

### 통합 검증

- 21개 training dataset으로 학습한 artifact의 feature/categorical 목록이 공통
  계약과 정확히 일치한다.
- evaluator, serving builder, simulation/daily, loader가 같은 canonical contract로
  함께 cutover되며, 이전 artifact가 padding 없이 거부된다.
- Feast test reader를 통한 `/rerank`가 신규 6개를 포함한 21개 DataFrame을
  모델에 전달한다.
- 21개 artifact를 사용한 일일 추천 경로가 후보 전체를 scoring한다.
- model artifact가 15개인 앱의 `/healthcheck`는 503이다.
- model artifact가 21개인 앱의 `/healthcheck`는 200이다.

## 완료 조건

- feature 이름과 순서의 production SSOT가 `src/features/model_contract.py`
  하나로 제한된다.
- 학습 artifact, Inference Server, 일일 추천/simulation이 동일한 21개 계약을
  사용한다.
- 신규 6개 feature가 정의된 출처와 default로 조립된다.
- 계약 불일치는 학습, startup 또는 batch 시작 경계에서 명확히 차단된다.
- 21개 input feature와 `clicked` label을 합한 22컬럼 dataset 설명이 코드,
  가이드, 테스트에서 일치한다.
- rollout 사전 조건과 champion promote 순서가 운영 담당자에게 인계된다.
