# Model Feature Contract SSOT와 21개 Feature 전환 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 학습, Inference Server, 일일 추천이 하나의 canonical 21개 model feature 계약을 사용하고, 계약이 다른 artifact를 실행 경계에서 차단한다.

**Architecture:** `src/features/model_contract.py`가 feature 이름·순서와 categorical 목록을 소유한다. 학습은 이 계약으로 DataFrame과 artifact를 만들고, online serving과 batch scoring은 같은 계약을 만족하는 feature vector를 조립한다. FeatureStore read refs는 파생용 보조 컬럼 때문에 serving 계층에 유지하되 contract 테스트로 canonical output을 보장한다.

**Tech Stack:** Python 3.12, pandas, LightGBM, FastAPI/Pydantic v2, Feast 0.64, MLflow, pytest, uv.

## Global Constraints

- 구현 worktree는 `C:\Autoresarch-worktrees\feat-251-feature-contract-ssot`, 브랜치는 `feat/251-feature-contract-ssot`를 사용한다.
- production model input은 정확히 21개이며 이름과 순서가 모두 계약이다.
- categorical feature는 `age_group`, `occupation`, `watch_time_band`, `historical_category_affinity`, `category_id` 5개다.
- `training_dataset.csv`는 21개 input feature와 `clicked` label을 합한 22컬럼이다.
- 신규 categorical 결측은 `"unknown"`, 신규 integer 결측은 `0`으로 조립한다.
- 15개와 21개 artifact를 영구적으로 이중 지원하지 않는다.
- FeatureView, FeatureStore build SQL, Airflow schedule과 모델 hyperparameter는 변경하지 않는다.
- 각 Task는 실패 테스트를 먼저 확인하고 최소 구현 후 targeted test를 통과시킨다.
- 사용자가 별도로 요청하기 전에는 commit, push, PR 생성 단계는 실행하지 않는다. 아래 commit 명령은 실행 시 승인 후 사용한다.

---

**Source spec:** `docs/specs/2026-07-22-feature-contract-ssot.md`

## 파일 소유권과 Task DAG

| Task | 산출물 | 주요 소유 파일 | 선행 Task |
| --- | --- | --- | --- |
| 1 | canonical 21개 계약 | `src/features/model_contract.py`, contract test | 없음 |
| 2 | SSOT 기반 학습·평가와 artifact | `train.py`, `evaluate.py`, `config.yaml`, train test | 1 |
| 3 | 21개 online serving 조립 | `online_features.py`, `app.py`, serving tests | 1 |
| 4 | 21개 simulation/daily scoring frame | `simulate_policy_round.py`, batch tests | 1 |
| 5 | artifact loader의 strict contract gate | `model_loader.py`, loader/API tests | 1, 2, 3, 4 |
| 6 | 가이드 정합과 전체 검증·rollout evidence | guides/spec, 전체 test | 1~5 |

```text
Task 1 canonical contract
  ├─ Task 2 training/evaluate
  ├─ Task 3 online serving
  └─ Task 4 simulation/daily batch
       └─ Task 5 artifact/startup gate
            └─ Task 6 docs, regression, rollout gate
```

Task 2~4는 Task 1 뒤에 파일 충돌 없이 진행할 수 있지만, 한 agent가 실행할 때는
리뷰와 원인 추적을 단순하게 유지하기 위해 번호 순서대로 진행한다.

## 구현 전 preflight

- [ ] **Step 1: 격리 worktree와 branch 확인**

```bash
git branch --show-current
git status --short
git worktree list
```

Expected: 현재 경로가 `C:/Autoresarch-worktrees/feat-251-feature-contract-ssot`, branch가
`feat/251-feature-contract-ssot`이며 기존 `C:/Autoresarch`의 변경은 이 status에
나타나지 않는다.

- [ ] **Step 2: baseline targeted tests 실행**

```bash
uv sync --frozen
uv run --no-sync python -m pytest \
  tests/test_pipeline_train.py \
  tests/test_serving_online_features.py \
  tests/test_serving_api.py \
  tests/test_simulate_policy_round.py \
  tests/test_daily_recommendations.py -v
```

Expected: 변경 전 전체 PASS. 실패가 있으면 구현을 시작하지 않고 pre-existing
failure의 test 이름과 traceback을 기록한다.

## Task 1: canonical 21개 model feature 계약 추가

**Files:**

- Create: `src/features/model_contract.py`
- Create: `tests/test_model_feature_contract.py`

**Interfaces:**

- Produces: `MODEL_FEATURE_COLUMNS: tuple[str, ...]`
- Produces: `CATEGORICAL_FEATURE_COLUMNS: tuple[str, ...]`
- Produces: `FeatureContractError(reason: str)`
- Produces: `require_model_feature_columns(columns: Sequence[str]) -> tuple[str, ...]`
- Produces: `require_categorical_feature_columns(columns: Sequence[str]) -> tuple[str, ...]`

- [ ] **Step 1: 정확한 순서와 strict validator 실패 테스트 작성**

```python
from __future__ import annotations

import pytest

from src.features.model_contract import (
    CATEGORICAL_FEATURE_COLUMNS,
    MODEL_FEATURE_COLUMNS,
    FeatureContractError,
    require_categorical_feature_columns,
    require_model_feature_columns,
)

EXPECTED_MODEL_FEATURE_COLUMNS = (
    "age_group",
    "occupation",
    "watch_time_band",
    "recent_click_count_7d",
    "recent_view_count_7d",
    "recent_watch_time_7d",
    "recent_like_count_7d",
    "historical_category_affinity",
    "total_event_count_7d",
    "category_id",
    "duration_sec",
    "view_count",
    "like_ratio",
    "comment_ratio",
    "days_since_upload",
    "channel_subscriber_count",
    "channel_view_count",
    "channel_video_count",
    "topic_similarity",
    "preferred_category_match",
    "historical_category_match",
)


def test_model_feature_contract_has_canonical_order() -> None:
    assert MODEL_FEATURE_COLUMNS == EXPECTED_MODEL_FEATURE_COLUMNS
    assert len(MODEL_FEATURE_COLUMNS) == len(set(MODEL_FEATURE_COLUMNS)) == 21


def test_categorical_contract_is_ordered_subset() -> None:
    assert CATEGORICAL_FEATURE_COLUMNS == (
        "age_group",
        "occupation",
        "watch_time_band",
        "historical_category_affinity",
        "category_id",
    )
    assert set(CATEGORICAL_FEATURE_COLUMNS) < set(MODEL_FEATURE_COLUMNS)


def test_contract_rejects_missing_or_reordered_columns() -> None:
    with pytest.raises(FeatureContractError):
        require_model_feature_columns(MODEL_FEATURE_COLUMNS[:-1])
    with pytest.raises(FeatureContractError):
        require_model_feature_columns(tuple(reversed(MODEL_FEATURE_COLUMNS)))
    with pytest.raises(FeatureContractError):
        require_categorical_feature_columns(CATEGORICAL_FEATURE_COLUMNS[:-1])
```

- [ ] **Step 2: 실패 확인**

Run:

```bash
uv run --no-sync python -m pytest tests/test_model_feature_contract.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'src.features.model_contract'`.

- [ ] **Step 3: 최소 계약 모듈 구현**

```python
"""학습과 추론이 공유하는 CTR 모델 입력 feature 계약.

전체 파이프라인에서 model input의 이름·순서와 categorical 분류를 소유한다.
Feature 값 계산, Feast 조회, artifact I/O는 소유하지 않는다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

MODEL_FEATURE_COLUMNS: Final[tuple[str, ...]] = (
    "age_group",
    "occupation",
    "watch_time_band",
    "recent_click_count_7d",
    "recent_view_count_7d",
    "recent_watch_time_7d",
    "recent_like_count_7d",
    "historical_category_affinity",
    "total_event_count_7d",
    "category_id",
    "duration_sec",
    "view_count",
    "like_ratio",
    "comment_ratio",
    "days_since_upload",
    "channel_subscriber_count",
    "channel_view_count",
    "channel_video_count",
    "topic_similarity",
    "preferred_category_match",
    "historical_category_match",
)
CATEGORICAL_FEATURE_COLUMNS: Final[tuple[str, ...]] = (
    "age_group",
    "occupation",
    "watch_time_band",
    "historical_category_affinity",
    "category_id",
)


@dataclass(frozen=True, slots=True)
class FeatureContractError(Exception):
    reason: str

    def __str__(self) -> str:
        return self.reason


def require_model_feature_columns(columns: Sequence[str]) -> tuple[str, ...]:
    actual = tuple(columns)
    if actual != MODEL_FEATURE_COLUMNS:
        raise FeatureContractError("Model feature columns do not match the canonical contract.")
    return actual


def require_categorical_feature_columns(columns: Sequence[str]) -> tuple[str, ...]:
    actual = tuple(columns)
    if actual != CATEGORICAL_FEATURE_COLUMNS:
        raise FeatureContractError("Categorical columns do not match the canonical contract.")
    return actual
```

- [ ] **Step 4: targeted test 통과**

```bash
uv run --no-sync python -m pytest tests/test_model_feature_contract.py -v
```

Expected: 3 tests PASS.

- [ ] **Step 5: 승인된 경우 Task commit**

```bash
git add src/features/model_contract.py tests/test_model_feature_contract.py
git commit -m "feat: 공통 모델 피처 계약을 추가"
```

## Task 2: 학습·평가를 SSOT와 21개 artifact에 연결

**Files:**

- Modify: `src/pipeline/config.yaml`
- Modify: `src/pipeline/train.py`
- Modify: `src/pipeline/evaluate.py`
- Modify: `tests/test_pipeline_train.py`
- Create: `tests/test_pipeline_evaluate.py`

**Interfaces:**

- Consumes: Task 1의 두 column tuple
- Produces: canonical 순서의 `feature_columns.pkl`
- Produces: canonical 5개 key 순서의 `categorical_columns.pkl`
- Preserves: config의 data path, split ratio, artifact path, model parameter 계약

- [ ] **Step 1: 21개 학습과 config 중복 제거 실패 테스트 작성**

`tests/test_pipeline_train.py`의 local `FEATURE_COLUMNS`와
`CATEGORICAL_COLUMNS`를 삭제하고 Task 1 상수를 import한다. synthetic dataset에
다음 값을 추가한다.

```python
"watch_time_band": rng.choice(["morning", "evening", "night", "unknown"], size=n),
"recent_view_count_7d": rng.integers(0, 30, size=n),
"total_event_count_7d": rng.integers(0, 100, size=n),
"channel_subscriber_count": rng.integers(0, 1_000_000, size=n),
"channel_view_count": rng.integers(0, 100_000_000, size=n),
"channel_video_count": rng.integers(0, 10_000, size=n),
```

test config의 `data`에는 `feature_columns`와 `categorical_columns`를 넣지 않는다.
학습 실행 후 artifact를 읽어 다음을 단정한다.

```python
with feature_columns_path.open("rb") as stream:
    assert tuple(pickle.load(stream)) == MODEL_FEATURE_COLUMNS
with categorical_columns_path.open("rb") as stream:
    categories = pickle.load(stream)
assert tuple(categories) == CATEGORICAL_FEATURE_COLUMNS
assert "watch_time_band" in categories
```

- [ ] **Step 2: config가 없는 평가 경로 실패 테스트 작성**

`tests/test_pipeline_evaluate.py`에서 `evaluate.load_feature_columns`가 canonical
21개를 반환하고, config의 `data`에 categorical 목록이 없는 fixture를 사용한다.
fake model의 `predict_proba`가 받은 frame을 보관하게 한 뒤 다음을 단정한다.

```python
evaluate.main(config_path=str(config_path), data_path=str(data_path))
assert tuple(fake_model.received.columns) == MODEL_FEATURE_COLUMNS
for column in CATEGORICAL_FEATURE_COLUMNS:
    assert str(fake_model.received[column].dtype) == "category"
```

- [ ] **Step 3: 실패 확인**

```bash
uv run --no-sync python -m pytest \
  tests/test_pipeline_train.py \
  tests/test_pipeline_evaluate.py -v
```

Expected: train/evaluate가 삭제된 config key를 읽어 `KeyError`로 FAIL.

- [ ] **Step 4: train과 evaluate를 공통 계약으로 변경**

두 module에 다음 import를 추가한다.

```python
from src.features.model_contract import (
    CATEGORICAL_FEATURE_COLUMNS,
    MODEL_FEATURE_COLUMNS,
)
```

`train.main()`의 config lookup을 다음으로 교체한다.

```python
feature_columns = list(MODEL_FEATURE_COLUMNS)
categorical_columns = list(CATEGORICAL_FEATURE_COLUMNS)
```

`evaluate.main()`은 artifact feature 목록을 계속 읽되 strict helper로 검증하고,
categorical cast에는 `CATEGORICAL_FEATURE_COLUMNS`를 사용한다.

```python
feature_columns = require_model_feature_columns(load_feature_columns(feature_columns_path))
X = dataset[list(feature_columns)].copy()
for column in CATEGORICAL_FEATURE_COLUMNS:
    X[column] = X[column].astype("category")
```

`config.yaml`에서는 `data.feature_columns`와 `data.categorical_columns` 두 block을
삭제한다. artifact 출력 경로는 그대로 유지한다.

- [ ] **Step 5: targeted tests 통과**

```bash
uv run --no-sync python -m pytest \
  tests/test_model_feature_contract.py \
  tests/test_pipeline_train.py \
  tests/test_pipeline_evaluate.py -v
```

Expected: 전체 PASS, 생성 artifact feature 수 21, categorical key 수 5.

- [ ] **Step 6: 승인된 경우 Task commit**

```bash
git add src/pipeline/config.yaml src/pipeline/train.py src/pipeline/evaluate.py \
  tests/test_pipeline_train.py tests/test_pipeline_evaluate.py
git commit -m "feat: 학습과 평가를 공통 피처 계약에 연결"
```

## Task 3: Inference Server가 21개 feature를 조회·조립하도록 전환

**Files:**

- Modify: `src/serving/online_features.py`
- Modify: `src/serving/app.py`
- Modify: `tests/test_serving_online_features.py`
- Modify: `tests/test_serving_api.py`

**Interfaces:**

- Consumes: Task 1의 constants, validator, `FeatureContractError`
- Produces: 각 `CandidateVideo.features`에 canonical 21개 값
- Preserves: 두 번의 keyed batch read, 입력 video 순서, typed retrieval errors

- [ ] **Step 1: 신규 read refs와 21개 output 실패 테스트 작성**

`tests/test_serving_online_features.py`의 first-read fixture와 기대 ref에 다음을
추가한다.

```python
"UserStaticView:watch_time_band",
"UserDynamicView:recent_view_count_7d",
"UserDynamicView:total_event_count_7d",
"VideoFeatureView:channel_subscriber_count",
"VideoFeatureView:channel_view_count",
"VideoFeatureView:channel_video_count",
```

fixture row에는 서로 구분되는 값을 넣고 결과를 단정한다.

```python
assert tuple(candidate.features) == MODEL_FEATURE_COLUMNS
assert candidate.features["watch_time_band"] == "night"
assert candidate.features["recent_view_count_7d"] == 7
assert candidate.features["total_event_count_7d"] == 31
assert candidate.features["channel_subscriber_count"] == 10_000
assert candidate.features["channel_view_count"] == 500_000
assert candidate.features["channel_video_count"] == 120
```

typed cold-start test에는 신규 값이 `None`일 때 `unknown`/`0`이 되는 assertion을
추가한다.

- [ ] **Step 2: app categorical 계약 실패 테스트 작성**

`tests/test_serving_api.py`에서 model artifact의 `watch_time_band` category 값이
문자열이 아니면 healthcheck 503임을 추가한다.

```python
resolved = make_resolved_model(
    feature_columns=MODEL_FEATURE_COLUMNS,
    categorical_categories={"watch_time_band": (1, 2)},
)
response = client_for(resolved).get("/healthcheck")
assert response.status_code == 503
```

- [ ] **Step 3: 실패 확인**

```bash
uv run --no-sync python -m pytest \
  tests/test_serving_online_features.py \
  tests/test_serving_api.py -v
```

Expected: 신규 refs/output과 `watch_time_band` 검사 부재로 FAIL.

- [ ] **Step 4: online builder를 공통 계약과 신규 6개에 연결**

`MODEL_FEATURE_COLUMNS`와 `FeatureContractError`의 local 정의를 삭제하고 Task 1
module에서 import한다. `_FIRST_READ_FEATURE_REFS`, `_FIRST_READ_COLUMNS`,
`_candidate_from_row()`에 신규 6개를 추가한다.

```python
"watch_time_band": _string_or_default(row["watch_time_band"], default="unknown"),
"recent_view_count_7d": _integer_or_default(row["recent_view_count_7d"]),
"total_event_count_7d": _integer_or_default(row["total_event_count_7d"]),
"channel_subscriber_count": _integer_or_default(row["channel_subscriber_count"]),
"channel_view_count": _integer_or_default(row["channel_view_count"]),
"channel_video_count": _integer_or_default(row["channel_video_count"]),
```

`_validate_build_request()`는 local tuple 비교 대신 다음을 호출한다.

```python
require_model_feature_columns(feature_columns)
```

최종 dict literal 순서는 `MODEL_FEATURE_COLUMNS`와 같게 배치하고 반환 직전에
다음 내부 assertion이 아니라 테스트로 순서를 고정한다.

- [ ] **Step 5: app의 categorical 목록을 공통 계약으로 교체**

local `STRING_CATEGORICAL_FEATURE_COLUMNS`를 삭제하고
`CATEGORICAL_FEATURE_COLUMNS`를 import한다. `unavailable_detail()`의 문자열 타입
검사는 5개 전체를 순회한다.

- [ ] **Step 6: targeted tests 통과**

```bash
uv run --no-sync python -m pytest \
  tests/test_model_feature_contract.py \
  tests/test_serving_online_features.py \
  tests/test_serving_api.py -v
```

Expected: 전체 PASS, builder 결과가 21개이며 두 read 호출 수는 그대로 2회.

- [ ] **Step 7: 승인된 경우 Task commit**

```bash
git add src/serving/online_features.py src/serving/app.py \
  tests/test_serving_online_features.py tests/test_serving_api.py
git commit -m "feat: 온라인 서빙 피처를 21개로 확장"
```

## Task 4: simulation과 일일 추천 frame을 21개로 전환

**Files:**

- Modify: `src/pipeline/simulate_policy_round.py`
- Modify: `tests/test_simulate_policy_round.py`
- Modify: `tests/test_daily_recommendations.py`

**Interfaces:**

- Consumes: Task 1의 canonical contract
- Produces: `build_pool_feature_frame()`의 canonical 21개 model columns
- Produces: `_to_candidate_videos()`의 strict artifact validation
- Preserves: `compute_video_features()`의 channel 통계 계산과 기존 ranking flow

- [ ] **Step 1: pool frame의 21개 실패 테스트 작성**

`tests/test_simulate_policy_round.py` fixture에 `watch_time_band`와 point-in-time
집계 입력을 넣고 다음을 단정한다.

```python
frame = build_pool_feature_frame(
    personas=personas,
    events=events,
    videos_raw=videos_raw,
    user_id="user-1",
    as_of="2026-07-22 00:00:00",
    snapshot_date="2026-07-22",
)
assert not (set(MODEL_FEATURE_COLUMNS) - set(frame.columns))
assert frame.loc[0, "watch_time_band"] == "night"
assert frame.loc[0, "recent_view_count_7d"] == expected_recent_views
assert frame.loc[0, "total_event_count_7d"] == expected_total_events
assert frame.loc[0, "channel_subscriber_count"] == expected_subscribers
```

artifact가 15개이거나 순서가 다르면 Candidate 변환이 실패하는 테스트도 추가한다.

```python
with pytest.raises(FeatureContractError):
    _to_candidate_videos(frame, MODEL_FEATURE_COLUMNS[:-1])
```

- [ ] **Step 2: daily recommendation이 21개 Candidate를 전달하는 실패 테스트 작성**

`tests/test_daily_recommendations.py`의 stub reranker가
`MODEL_FEATURE_COLUMNS`를 사용하게 하고 전달된 각 candidate에 대해 단정한다.

```python
assert tuple(stub_reranker.received[0].features) == MODEL_FEATURE_COLUMNS
```

- [ ] **Step 3: 실패 확인**

```bash
uv run --no-sync python -m pytest \
  tests/test_simulate_policy_round.py \
  tests/test_daily_recommendations.py -v
```

Expected: frame에서 신규 user feature가 누락되고 15개 artifact가 허용되어 FAIL.

- [ ] **Step 4: pool frame과 Candidate 변환 구현**

offline 복사 목록에 `watch_time_band`, online 복사 목록에
`recent_view_count_7d`와 `total_event_count_7d`를 추가한다.

```python
for column in ("age_group", "occupation", "watch_time_band"):
    frame[column] = user_offline.iloc[0][column]

for column in (
    "historical_category_affinity",
    "recent_click_count_7d",
    "recent_view_count_7d",
    "recent_watch_time_7d",
    "recent_like_count_7d",
    "total_event_count_7d",
):
    frame[column] = online.iloc[0][column]
```

`compute_video_features()`가 이미 `channel_*` 3개를 반환하므로 중복 계산하지
않는다. `_to_candidate_videos()` 시작에서 artifact columns를 검증한다.

```python
columns = require_model_feature_columns(feature_columns)
```

각 row는 `columns` 순서로 dict를 만든다. contract 오류는 user 단위 skip 대상이
아니므로 `daily_recommendations.main()`의 user loop 전에 한 번 검증한다.

- [ ] **Step 5: targeted tests 통과**

```bash
uv run --no-sync python -m pytest \
  tests/test_features_assembly.py \
  tests/test_simulate_policy_round.py \
  tests/test_daily_recommendations.py -v
```

Expected: 전체 PASS, daily reranker가 21개 ordered feature를 받음.

- [ ] **Step 6: 승인된 경우 Task commit**

```bash
git add src/pipeline/simulate_policy_round.py \
  tests/test_simulate_policy_round.py tests/test_daily_recommendations.py
git commit -m "feat: 일일 추천 피처를 공통 계약에 연결"
```

## Task 5: model artifact loader에 strict contract gate 추가

**Files:**

- Modify: `src/serving/model_loader.py`
- Modify: `tests/test_serving_api.py`
- Modify: `tests/test_serving_model_registry.py`

**Interfaces:**

- Consumes: Task 1의 두 validator
- Produces: canonical 계약이 아니면 `ModelArtifactError`인 local/registry loader
- Preserves: artifact 형식 오류와 category value 복원 동작

- [ ] **Step 1: 15개와 reordered 21개 artifact 거부 테스트 작성**

기존 local loader test helper로 model과 pickle 세 개를 만들고 다음을 추가한다.

```python
@pytest.mark.parametrize(
    "feature_columns",
    [MODEL_FEATURE_COLUMNS[:-1], tuple(reversed(MODEL_FEATURE_COLUMNS))],
)
def test_local_model_loader_rejects_noncanonical_feature_contract(
    tmp_path: Path, feature_columns: tuple[str, ...]
) -> None:
    paths = write_model_artifacts(
        tmp_path,
        feature_columns=feature_columns,
        categorical_categories=canonical_categories(),
    )
    with pytest.raises(ModelArtifactError, match="canonical contract"):
        load_local_model(LocalModelSettings(*paths))
```

categorical key 누락, key 순서 변경, `watch_time_band` 누락도 거부한다.

```python
with pytest.raises(ModelArtifactError, match="Categorical columns"):
    load_local_model(settings_with_categories_without_watch_time_band)
```

- [ ] **Step 2: 실패 확인**

```bash
uv run --no-sync python -m pytest \
  tests/test_serving_api.py -k "model_loader and contract" -v
```

Expected: 현재 loader는 비어 있지 않고 subset이기만 하면 허용하므로 FAIL.

- [ ] **Step 3: artifact parse 직후 canonical 검증 구현**

Pydantic adapter가 형식을 검증한 뒤 공통 validator를 호출하고, domain error를
loader error로 변환한다.

```python
try:
    feature_columns = require_model_feature_columns(feature_columns)
    require_categorical_feature_columns(tuple(categorical_categories))
except FeatureContractError as error:
    raise ModelArtifactError(reason=str(error)) from error
```

기존 `unknown_columns` subset 검사는 strict categorical 검증에 포함되므로 제거한다.
`Reranker.feature_columns`에는 검증된 tuple을 그대로 전달한다.

- [ ] **Step 4: app 주입 경로의 healthcheck 방어 유지 확인**

테스트가 loader를 우회해 `ResolvedModel`을 직접 주입할 수 있으므로
`app.unavailable_detail()`의 feature/categorical direct 비교는 유지한다.
다음 기존 테스트가 계속 PASS해야 한다.

```bash
uv run --no-sync python -m pytest \
  tests/test_serving_api.py -k "healthcheck or model_loader" -v
```

- [ ] **Step 5: registry와 local targeted tests 통과**

```bash
uv run --no-sync python -m pytest \
  tests/test_serving_api.py \
  tests/test_serving_model_registry.py -v
```

Expected: 전체 PASS. 15개/reordered artifact는 model load 단계에서 거부됨.

- [ ] **Step 6: 승인된 경우 Task commit**

```bash
git add src/serving/model_loader.py tests/test_serving_api.py \
  tests/test_serving_model_registry.py
git commit -m "feat: 모델 아티팩트 계약을 로드 시점에 검증"
```

## Task 6: 문서 정합, 전체 회귀와 rollout gate

**Files:**

- Modify: `docs/guides/training-dataset.md`
- Modify: `docs/guides/ctr-model-specification.md`
- Modify: `docs/specs/2026-07-16-reranking-serving-api.md`
- Modify: `docs/specs/2026-07-22-feature-contract-ssot.md`
- Modify: `src/pipeline/build_training_dataset.py` module output description only
- No behavior change outside documentation corrections

**Interfaces:**

- Consumes: Task 1~5의 최종 behavior
- Produces: 21 input + label = 22 columns 설명과 운영 cutover checklist

- [ ] **Step 1: 문서의 15개/21컬럼 표현을 최종 계약으로 정리**

다음을 명시한다.

```text
Model input: 21 features
Training dataset: 21 features + clicked label = 22 columns
Canonical source: src/features/model_contract.py
Online source: 4 FeatureViews, two keyed batch reads
```

`build_training_dataset.py` 상단의 “21컬럼” 표현은 “21개 model input + clicked,
총 22컬럼”으로 바꾼다. 이전 15개 serving 계약 설명은 역사/rollout 문맥이 아니면
삭제한다.

- [ ] **Step 2: contract 관련 전체 targeted suite 실행**

```bash
uv run --no-sync python -m pytest \
  tests/test_model_feature_contract.py \
  tests/test_build_training_dataset.py \
  tests/test_pipeline_train.py \
  tests/test_pipeline_evaluate.py \
  tests/test_features_assembly.py \
  tests/test_simulate_policy_round.py \
  tests/test_daily_recommendations.py \
  tests/test_serving_online_features.py \
  tests/test_serving_api.py \
  tests/test_serving_model_registry.py -v
```

Expected: 전체 PASS.

- [ ] **Step 3: dev 전체 회귀와 정적 검증**

```bash
uv run --no-sync python -m pytest -v
uv lock --check
git diff --check
```

Expected: exit 0. pre-existing failure가 있으면 이번 변경과의 인과 여부를 분리해
PR에 test 이름과 traceback을 기록한다.

- [ ] **Step 4: Feast 격리 회귀**

```bash
uv sync --frozen --no-dev --group feast
uv run --no-sync python -m pytest \
  tests/test_serving_feast_reader.py \
  tests/test_serving_online_features.py \
  tests/test_serving_api.py -v
```

Expected: 실제 Redis 없이 전체 PASS.

- [ ] **Step 5: rollout 전 운영 evidence 확보**

아래 조건을 모두 확인하기 전에는 21개 모델을 `@champion`으로 promote하지 않는다.

1. BigQuery FeatureStore source 3종에 신규 6개 컬럼이 있다.
2. Redis materialize 이후 존재하는 user/video를 21개로 조회할 수 있다.
3. 21개 artifact의 offline 평가가 승인됐다.
4. 새 serving/batch image tag가 배포 가능하다.
5. 실행 중 pod가 alias 변경을 자동 reload하지 않는지 확인했다.
6. 일일 추천 schedule을 cutover 동안 중지하거나 새 image 사용을 보장했다.

cutover 순서:

```text
21개 모델 registry 등록(아직 champion 아님)
→ feature/serving smoke
→ daily schedule gate
→ champion promote
→ serving rolling rollout
→ /healthcheck 200 + /rerank smoke
→ daily recommendation 1회 성공
→ schedule 재개
```

- [ ] **Step 6: spec 상태와 검증 결과 갱신**

`docs/specs/2026-07-22-feature-contract-ssot.md`의 상태를 `Implemented`로 바꾸는
조건은 Task 1~5 코드와 Step 2~4 자동 검증이 모두 통과한 때다. 운영 smoke가 아직
남으면 상태 아래에 `Production cutover pending`과 미완료 gate를 명시한다.

- [ ] **Step 7: 승인된 경우 Task commit**

```bash
git add docs/guides/training-dataset.md \
  docs/guides/ctr-model-specification.md \
  docs/specs/2026-07-16-reranking-serving-api.md \
  docs/specs/2026-07-22-feature-contract-ssot.md \
  src/pipeline/build_training_dataset.py
git commit -m "docs: 21개 모델 피처 전환 계약을 정리"
```

## 완료 기준

- `src/features/model_contract.py`만 model feature 이름·순서와 categorical 목록을
  정의한다.
- train/evaluate/serving/simulation/daily가 같은 21개 tuple을 소비한다.
- 학습 artifact의 feature 순서와 categorical key가 canonical 계약과 일치한다.
- online builder는 신규 6개를 정의된 Feast source와 typed default로 조립한다.
- simulation/daily frame도 같은 21개를 제공한다.
- 15개 또는 reordered artifact는 load/startup/batch 경계에서 실패한다.
- training dataset 문서는 21 input + `clicked` = 22 columns로 일치한다.
- 전체 dev 및 Feast 격리 회귀가 통과한다.
- champion promote 전에 rollout gate와 rollback 조건이 운영 담당자에게 전달된다.

## 롤백

21개 champion promote 전에는 이전 15개 모델과 image가 그대로 유지되므로 alias를
변경하지 않는다. promote 후 문제가 발생하면 serving과 batch를 이전 image로
되돌리고 `@champion` alias를 이전 15개 model version으로 복원한다. FeatureStore
스키마와 값은 additive 상태이므로 데이터 롤백은 필요하지 않다. rollback 후에는
이슈 #251을 닫지 않고 실패한 gate와 model run ID를 기록한다.

