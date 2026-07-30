# CTRModel 인터페이스 포팅 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `CTRModel` 추상 인터페이스를 main에 포팅하고 `LGBMModel`이 이를 구현하도록
리팩터해서, 기존 저장 아티팩트 포맷을 바꾸지 않으면서 향후 모델 확장의 저장 계약을
자동 테스트로 보장한다.

**Architecture:** `src/models/base.py`에 `CTRModel(ABC)`를 신설하고, `LGBMModel`이
이를 상속하며 `save`/`load`를 override해 기존 raw-booster 저장 방식을 그대로
유지한다. `src/pipeline/train.py`의 저장 호출 1곳만 `model.save(model_path)`로
바꾼다. `FMModel`/`MLPModel` 포팅, 하이퍼파라미터 스키마 마이그레이션, 전처리 유틸
추출은 범위 밖(설계 문서에 컨벤션만 기록).

**Tech Stack:** Python 3.11/3.12, `abc`(표준 라이브러리), `lightgbm`, `joblib`,
`pytest`.

## Global Constraints

- 기존 저장 아티팩트 포맷(raw `lgb.LGBMClassifier` 객체를 joblib으로 저장)이
  바뀌면 안 된다 — `src/serving/model_loader.py`가 이 포맷을 그대로 기대한다.
- `model_loader.py`, ONNX 변환(`convert_lgbm_to_onnx`), `src/utils/model_utils.py`는
  이 플랜에서 수정하지 않는다.
- 새 파일의 모듈 최상단 docstring은 `.claude/docs/agent-python-reference.md`의
  Module Responsibility 형식([파이프라인]/[기능]/[비책임])을 따른다.
- 커밋 메시지 형식은 `<type>: <한국어 설명>` (`.claude/docs/agent-workflow-reference.md`).
- 관련 설계 문서: `docs/archive/specs/2026-07-30-ctr-model-interface-port.md`. 관련 이슈: #424.

---

### Task 1: `CTRModel` 추상 인터페이스 + 기본 save/load 계약 테스트

**Files:**
- Create: `src/models/base.py`
- Test: `tests/test_models_contract.py`

**Interfaces:**
- Produces: `src.models.base.CTRModel` — ABC. 하위 클래스는 `fit(self, X_train:
  pd.DataFrame, y_train: pd.Series, categorical_features: Optional[list] = None)
  -> None`과 `predict_proba(self, X: pd.DataFrame) -> np.ndarray`를 반드시
  구현해야 한다(구현 안 하면 인스턴스화 시 `TypeError`). `save(self, path: str)
  -> None`과 `classmethod load(cls, path: str) -> "CTRModel"`은 joblib 기반
  기본 구현을 제공하며 override 가능하다.

- [x] **Step 1: 실패하는 테스트 작성**

`tests/test_models_contract.py`를 새로 만든다.

```python
"""CTRModel 인터페이스를 구현한 모델들의 저장 계약 테스트.

새 모델 구현체가 save()를 override하든(LGBMModel) 안 하든(기본 구현) 저장→로드
후 predict_proba가 서빙이 기대하는 shape으로 동작하는지 검증한다.
"""

import numpy as np
import pandas as pd
import pytest

from src.models.base import CTRModel


def _tiny_dataset() -> tuple[pd.DataFrame, pd.Series]:
    X = pd.DataFrame(
        {
            "num_feature": [0.1, 0.5, 0.9, 0.3, 0.7, 0.2, 0.8, 0.4],
            "cat_feature": ["a", "b", "a", "b", "a", "b", "a", "b"],
        }
    )
    y = pd.Series([0, 1, 0, 1, 0, 1, 0, 1])
    return X, y


def test_ctr_model_cannot_be_instantiated_directly() -> None:
    with pytest.raises(TypeError):
        CTRModel()


class _DummyMeanModel(CTRModel):
    """save()/load()를 override하지 않고 CTRModel 기본 구현만 쓰는 테스트 전용 더미."""

    def __init__(self) -> None:
        self._mean = 0.5

    def fit(self, X_train, y_train, categorical_features=None) -> None:
        self._mean = float(y_train.mean())

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        p1 = np.full(len(X), self._mean)
        return np.column_stack([1 - p1, p1])


def test_ctr_model_default_save_load_round_trip_preserves_predict_proba(tmp_path) -> None:
    X, y = _tiny_dataset()
    model = _DummyMeanModel()
    model.fit(X, y)

    model_path = tmp_path / "dummy_model.joblib"
    model.save(str(model_path))

    loaded = CTRModel.load(str(model_path))

    assert isinstance(loaded, _DummyMeanModel)
    proba = loaded.predict_proba(X)
    assert proba.shape == (len(X), 2)
    np.testing.assert_allclose(proba[:, 1], y.mean())


def test_ctr_model_load_missing_file_raises_file_not_found_error(tmp_path) -> None:
    missing_path = tmp_path / "does_not_exist.joblib"
    with pytest.raises(FileNotFoundError):
        CTRModel.load(str(missing_path))
```

- [x] **Step 2: 테스트 실행해서 실패 확인**

Run: `uv run python -m pytest tests/test_models_contract.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.models.base'`

- [x] **Step 3: `src/models/base.py` 구현**

```python
"""CTR 이진 분류 모델 계열의 최소 추상 인터페이스.

[파이프라인] 학습(src/pipeline/train.py)이 모델 구현체(LGBMModel 등)를 다형적으로
다루기 위한 계약만 이 모듈이 정의한다. 각 구현체의 알고리즘·하이퍼파라미터·전처리는
개별 모듈(lgbm_model.py 등)이 소유한다.

[기능] fit/predict_proba를 각 구현체가 반드시 구현하도록 강제하고, save/load는
joblib 직렬화 기본 구현을 제공해 구현체가 필요할 때만 override하게 한다.

[비책임] 모델 서빙(src/serving/*), ONNX 변환(src.utils.model_utils
.convert_lgbm_to_onnx), MLflow 아티팩트 로깅(src/pipeline/train.py)은 이 인터페이스를
소비하지 않는다 — 기존 LightGBM 프로덕션 저장 경로는 이 인터페이스 도입으로 바뀌지
않는다(additive, capability probe round_001/round_002 검증을 거쳐 포팅,
docs/specs/2026-07-30-ctr-model-interface-port.md 참고).
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Optional

import numpy as np
import pandas as pd


class CTRModel(ABC):
    """CTR 이진 분류 모델이 공통으로 따르는 최소 인터페이스."""

    @abstractmethod
    def fit(
        self,
        X_train: pd.DataFrame,
        y_train: pd.Series,
        categorical_features: Optional[list] = None,
    ) -> None:
        """모델을 학습한다."""
        raise NotImplementedError

    @abstractmethod
    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        """클릭 확률을 예측한다.

        Returns:
            (n_samples, 2) shape. 각 행: [P(click=0), P(click=1)]
        """
        raise NotImplementedError

    def save(self, path: str) -> None:
        """joblib으로 self 전체를 직렬화하는 기본 구현.

        구현체가 다른 저장 방식(예: 프레임워크 네이티브 객체만 저장)이 필요하면
        override한다(LGBMModel이 그 예).
        """
        import joblib

        os.makedirs(os.path.dirname(path), exist_ok=True)
        joblib.dump(self, path)

    @classmethod
    def load(cls, path: str) -> "CTRModel":
        """joblib으로 역직렬화하는 기본 구현."""
        import joblib

        if not os.path.exists(path):
            raise FileNotFoundError(f"모델 파일을 찾을 수 없습니다: {path}")
        return joblib.load(path)
```

- [x] **Step 4: 테스트 실행해서 통과 확인**

Run: `uv run python -m pytest tests/test_models_contract.py -v`
Expected: PASS (3 tests)

- [x] **Step 5: lint 확인**

Run: `uv run --no-sync ruff check src/models/base.py tests/test_models_contract.py`
Expected: `All checks passed!`

- [x] **Step 6: 커밋**

```bash
git add src/models/base.py tests/test_models_contract.py
git commit -m "feat: CTRModel 추상 인터페이스 포팅 (#424)"
```

---

### Task 2: `LGBMModel`이 `CTRModel`을 구현하도록 리팩터

**Files:**
- Modify: `src/models/lgbm_model.py`
- Test: `tests/test_models_contract.py` (Task 1에서 만든 파일에 이어서 추가)

**Interfaces:**
- Consumes: `src.models.base.CTRModel` (Task 1)
- Produces: `LGBMModel.save(path: str) -> None`, `LGBMModel.load(path: str) ->
  LGBMModel`(classmethod) — 이후 Task 3이 `save()`를 호출한다.

- [x] **Step 1: 실패하는 테스트 작성**

`tests/test_models_contract.py`에 이어서 추가:

```python
def test_lgbm_model_save_load_round_trip_matches_serving_contract(tmp_path) -> None:
    import joblib
    import lightgbm as lgb

    from src.models.lgbm_model import LGBMModel

    X, y = _tiny_dataset()
    # LightGBM 4.x는 categorical_feature로 지정한 컬럼이 pandas "category" dtype이어야
    # 한다(object dtype은 ValueError) — 프로덕션 경로(src/pipeline/train.py의
    # collect_categorical_categories)와 동일한 캐스팅을 테스트 픽스처에도 적용한다.
    X = X.assign(cat_feature=X["cat_feature"].astype("category"))
    model = LGBMModel(scale_pos_weight=1.0, n_estimators=5, num_leaves=3, random_state=42)
    model.fit(X, y, categorical_features=["cat_feature"])

    model_path = tmp_path / "model.joblib"
    model.save(str(model_path))

    # 서빙(src/serving/model_loader.py)과 동일한 로드 경로 — joblib.load를 직접 호출한다.
    loaded = joblib.load(model_path)

    assert isinstance(loaded, lgb.LGBMClassifier)
    proba = loaded.predict_proba(X)
    assert proba.shape == (len(X), 2)


def test_lgbm_model_load_classmethod_round_trip(tmp_path) -> None:
    from src.models.lgbm_model import LGBMModel

    X, y = _tiny_dataset()
    X = X.assign(cat_feature=X["cat_feature"].astype("category"))
    model = LGBMModel(scale_pos_weight=1.0, n_estimators=5, num_leaves=3, random_state=42)
    model.fit(X, y, categorical_features=["cat_feature"])

    model_path = tmp_path / "model.joblib"
    model.save(str(model_path))

    loaded = LGBMModel.load(str(model_path))

    assert isinstance(loaded, LGBMModel)
    proba = loaded.predict_proba(X)
    assert proba.shape == (len(X), 2)
```

- [x] **Step 2: 테스트 실행해서 실패 확인**

Run: `uv run python -m pytest tests/test_models_contract.py -v`
Expected: FAIL — `AttributeError: 'LGBMModel' object has no attribute 'save'`
(`LGBMModel`이 아직 `CTRModel`을 상속하지 않음)

- [x] **Step 3: `src/models/lgbm_model.py` 수정**

파일 최상단 docstring과 클래스 선언부를 아래처럼 바꾼다(기존 `fit`/`predict_proba`/
`predict` 본문은 그대로 유지, `save`/`load`만 추가):

```python
"""LightGBM 모델 wrapper.

[파이프라인] 학습(src/pipeline/train.py)이 사용하는 champion 계열 모델 구현체.
model_contract 스칼라 피처를 축정렬 분할(tree split)로 학습한다.

[기능] src.models.base.CTRModel 인터페이스(fit/predict_proba/save/load)를 구현해,
향후 train.py가 다른 모델 구현체와 다형적으로 다룰 수 있게 한다. save/load는 기존
프로덕션 경로(src.utils.model_utils.save_model/load_model이 raw LightGBM booster를
직접 joblib 저장)와 동일한 결과를 내도록 override한다 — 인터페이스 추가가 기존 저장
아티팩트 포맷을 바꾸지 않는다(additive,
docs/specs/2026-07-30-ctr-model-interface-port.md 참고).

[비책임] ONNX 변환은 여전히 src.utils.model_utils.convert_lgbm_to_onnx가 전담한다
(이 클래스는 변환하지 않는다).
"""

import lightgbm as lgb
import numpy as np
import pandas as pd

from src.models.base import CTRModel


class LGBMModel(CTRModel):
    """LightGBM 이진 분류 모델 wrapper."""

    # ... 기존 __init__/fit/predict_proba/predict는 그대로 유지 ...

    def save(self, path: str) -> None:
        """raw LightGBM booster(self.model)를 joblib으로 저장한다.

        기존 src.pipeline.train이 호출해온 save_model(model.model, model_path)와
        동일한 결과물(raw booster, wrapper 아님)을 만든다 — 서빙(model_loader)이
        이 포맷을 그대로 기대하므로 아티팩트 포맷은 바뀌지 않는다.
        """
        if self.model is None:
            raise ValueError("모델이 학습되지 않았습니다.")
        from src.utils.model_utils import save_model

        save_model(self.model, path)

    @classmethod
    def load(cls, path: str) -> "LGBMModel":
        """joblib으로 저장된 raw LightGBM booster를 불러와 wrapper에 담는다."""
        from src.utils.model_utils import load_model

        instance = cls(scale_pos_weight=1)
        instance.model = load_model(path)
        return instance
```

클래스 선언을 `class LGBMModel(CTRModel):`로 바꾸고 import에
`from src.models.base import CTRModel`를 추가하는 것, 그리고 `save`/`load`
메서드를 클래스 끝에 추가하는 것 외에는 기존 코드를 수정하지 않는다.

- [x] **Step 4: 테스트 실행해서 통과 확인**

Run: `uv run python -m pytest tests/test_models_contract.py -v`
Expected: PASS (5 tests)

- [x] **Step 5: lint 확인**

Run: `uv run --no-sync ruff check src/models/lgbm_model.py tests/test_models_contract.py`
Expected: `All checks passed!`

- [x] **Step 6: 커밋**

```bash
git add src/models/lgbm_model.py tests/test_models_contract.py
git commit -m "feat: LGBMModel이 CTRModel 인터페이스를 구현하도록 리팩터 (#424)"
```

---

### Task 3: `train.py`가 `model.save()`를 쓰도록 통합 + 회귀 검증

**Files:**
- Modify: `src/pipeline/train.py:50` (import), `src/pipeline/train.py:487` (호출부)

**Interfaces:**
- Consumes: `LGBMModel.save(path: str) -> None` (Task 2)

- [x] **Step 1: 저장 호출부 교체**

`src/pipeline/train.py`의 487번째 줄:

```python
# 변경 전
save_model(model.model, model_path)

# 변경 후
model.save(model_path)
```

- [x] **Step 2: 이제 쓰이지 않는 import 제거**

`src/pipeline/train.py` 46~51번째 줄:

```python
# 변경 전
from src.utils.model_utils import (  # noqa: E402
    convert_lgbm_to_onnx,
    save_categorical_columns,
    save_feature_columns,
    save_model,
)

# 변경 후
from src.utils.model_utils import (  # noqa: E402
    convert_lgbm_to_onnx,
    save_categorical_columns,
    save_feature_columns,
)
```

(`save_model`은 이제 `LGBMModel.save()` 내부에서만 import되므로 여기서는 미사용이 된다.)

- [x] **Step 3: 기존 학습 파이프라인 테스트 전체 실행 — 회귀 확인**

Run: `uv run python -m pytest tests/test_pipeline_train.py -v`
Expected: 전체 PASS, 특히
`test_main_logs_onnx_artifact_and_serving_loads_it`(ONNX 아티팩트 로깅 +
서빙 로더가 joblib을 그대로 읽는 경로)와 `test_main_downsampling_records_
sampling_rate_and_preserves_test_set` 계열이 기존과 동일하게 통과해야 한다 —
저장 아티팩트 바이트가 바뀌지 않았다는 직접적인 증거.

- [x] **Step 4: 새 계약 테스트도 함께 재확인**

Run: `uv run python -m pytest tests/test_models_contract.py -v`
Expected: PASS (5 tests)

- [x] **Step 5: lint 확인**

Run: `uv run --no-sync ruff check src/pipeline/train.py`
Expected: `All checks passed!` (미사용 import 경고 없음)

- [x] **Step 6: 전체 스위트 실행**

Run: `uv run python -m pytest -v`
Expected: 기존 실패 목록(관련 없는 사전 실패) 외 신규 실패 없음

- [x] **Step 7: 커밋**

```bash
git add src/pipeline/train.py
git commit -m "refactor: train.py가 LGBMModel.save()로 저장하도록 통합 (#424)"
```

---

## Self-Review 체크리스트 (구현 착수 전 확인 완료)

- **Spec coverage**: 설계 문서의 "완료 조건" 중 첫 2개(`CTRModel` 인터페이스 + 저장
  계약 테스트)는 Task 1·2·3이 구현한다. 나머지 3개(중첩 스키마/전처리 가이드/
  FM·MLP 제외)는 이미 `docs/archive/specs/2026-07-30-ctr-model-interface-port.md`
  문서 자체로 충족되어 있어 별도 코드 태스크가 필요 없다.
- **Placeholder scan**: 없음 — 모든 스텝에 실제 코드/명령이 있다.
- **Type consistency**: `CTRModel.fit`/`predict_proba` 시그니처가 Task 1~2 전체에서
  동일하게 쓰인다. `LGBMModel.save`/`load`가 Task 3에서 호출하는 이름과 일치한다.

## 다음 단계 (이 플랜 범위 밖)

3개 태스크가 모두 끝나고 전체 테스트가 통과하면, `.claude/docs/agent-workflow-reference.md`의
PR 워크플로우(`gh pr create`, `Closes #424`)로 리뷰를 요청한다.
