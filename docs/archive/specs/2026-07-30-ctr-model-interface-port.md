# CTRModel 인터페이스 포팅 — 설계 문서

> Status: Implemented | Issue: #424 | Branch: `feat/424-ctr-model-interface-port`

## 배경

Capability probe round_001(FM vs LightGBM)/round_002(MLP vs LightGBM)에서 로컬
브랜치 `worktree-agent-capability-probe`(main 대비 13 커밋, 미병합)에 모델 추상
인터페이스 `CTRModel`(`fit`/`predict_proba`/`save`/`load`)과 `FMModel`/`MLPModel`
challenger 구현체를 도입해 실제로 확장성을 검증했습니다.

검증 결과 3개 문제가 드러났습니다(#424에 상세 기록):

1. 저장 포맷이 인터페이스 계약과 맞는지 자동 검증이 없다
2. 모델별 하이퍼파라미터 네임스페이스가 표준화돼 있지 않다(`fm_*`/`mlp_*` flat 접두사)
3. 모델 간 전처리/인코딩 로직 공유 메커니즘이 없다(FM/MLP가 약 40줄을 각각 중복 구현)

## 범위 결정

이 문서가 다루는 범위는 **`CTRModel` 인터페이스와 `LGBMModel` 리팩터 포팅만**입니다.
`FMModel`/`MLPModel`은 포팅하지 않고 `worktree-agent-capability-probe` 브랜치에
참고용으로 남겨둡니다.

**이유**: FM/MLP는 저장 포맷 검증·서빙 연동·ONNX 변환이 모두 범위 밖으로 명시된
probe 전용 구현입니다. 검증되지 않은 실험 코드를 지금 prod 소스트리에 들이는 것보다,
인터페이스·계약 테스트·컨벤션 문서만 먼저 자리잡고 두 번째 실사용 모델이 실제로
필요해지는 시점에 그 모델과 함께 다시 판단하는 편이 되돌리기 쉽습니다(#424 코멘트
참고).

같은 이유로, 문제2(하이퍼파라미터 네임스페이스)와 문제3(전처리 공유)은 **실제
마이그레이션/추출 없이 컨벤션만 문서화**합니다 — 검증할 두 번째 모델이 없는 상태에서
스키마를 확정 짓지 않습니다.

## 아키텍처

### `src/models/base.py` (신규)

```python
class CTRModel(ABC):
    @abstractmethod
    def fit(self, X_train, y_train, categorical_features=None) -> None: ...

    @abstractmethod
    def predict_proba(self, X) -> np.ndarray: ...

    def save(self, path: str) -> None:
        """joblib으로 self 전체를 직렬화하는 기본 구현."""

    @classmethod
    def load(cls, path: str) -> "CTRModel":
        """joblib으로 역직렬화하는 기본 구현."""
```

`worktree-agent-capability-probe`의 `src/models/base.py`를 이 저장소 docstring
컨벤션(Module Responsibility: 담당 구간/제공 기능/비책임)에 맞춰 그대로 포팅합니다.
`save`/`load` 기본 구현은 구현체가 필요할 때만 override합니다.

### `src/models/lgbm_model.py` (변경)

`LGBMModel`이 `CTRModel`을 상속하도록 바꾸고 `save`/`load`를 override합니다.

```python
def save(self, path: str) -> None:
    """raw LightGBM booster(self.model)를 joblib으로 저장한다.

    기존 save_model(model.model, model_path) 호출과 동일한 결과물(raw
    booster, wrapper 아님)을 만든다 — 서빙(model_loader)이 이 포맷을
    그대로 기대하므로 아티팩트 포맷은 바뀌지 않는다.
    """
    from src.utils.model_utils import save_model
    save_model(self.model, path)

@classmethod
def load(cls, path: str) -> "LGBMModel":
    from src.utils.model_utils import load_model
    instance = cls(scale_pos_weight=1)
    instance.model = load_model(path)
    return instance
```

`fit`/`predict_proba`/`predict`는 기존 그대로 유지합니다(동작 변경 없음).

### `src/pipeline/train.py` (변경, 1줄)

486번째 줄의 저장 호출부만 인터페이스 메서드로 교체합니다.

```python
# 변경 전
save_model(model.model, model_path)

# 변경 후
model.save(model_path)
```

`save_model`을 여전히 내부에서 호출하므로 디스크에 쓰이는 바이트는 동일합니다.
`src/utils/model_utils.py`, `model_loader.py`, ONNX 변환 경로는 변경하지 않습니다.

### 서빙 경로에 대한 영향 — 없음

`model_loader.py`는 `LGBMModel`을 전혀 거치지 않고 `joblib.load(model_path)`를
직접 호출해 raw booster를 읽습니다(`_load_reranker`, 476번째 줄). 이번 변경은 그
경로를 건드리지 않습니다.

## 테스트 설계

`tests/test_models_contract.py` (신규) — "`CTRModel`을 구현한 모델이 저장→로드
후 서빙이 기대하는 대로 동작하는가"를 검증합니다.

1. **`LGBMModel` 저장 계약**: 더미 데이터로 `fit()` → `save(tmp_path)` →
   `joblib.load(tmp_path)`(서빙과 동일 경로)로 재로드 → `predict_proba(X)`가
   `(n, 2)` shape을 반환하는지 확인. 저장된 객체가 `lgb.LGBMClassifier`
   인스턴스인지(기존 아티팩트 포맷 불변)도 확인합니다.
2. **테스트 전용 더미 `CTRModel` 구현**: `save()`를 override하지 않는 기본
   경로(래퍼 전체 joblib 직렬화)로 저장→로드해도 `predict_proba`가 올바른
   shape을 반환하는지 확인. 이 케이스가 "override 없이도 계약이 깨지지 않는다"는
   근거가 되어, 다음에 추가될 모델이 이 패턴을 따라도 안전함을 보장합니다.

FM/MLP 자체에 대한 테스트, `model_loader.py`의 기존 테스트는 대상이 아닙니다.

## 향후 컨벤션 (지금 적용하지 않음, 문서로만 남김)

다음 모델이 실제로 추가될 때 아래 컨벤션을 따릅니다.

### 하이퍼파라미터 네임스페이스

`config["model"]["<type>"]["<param>"]` 중첩 스키마를 사용합니다.

```yaml
model:
  type: lightgbm  # lightgbm | fm | mlp | ...
  lightgbm:
    n_estimators: 200
    learning_rate: 0.05
  fm:
    n_factors: 8
    n_epochs: 200
```

probe 브랜치가 썼던 `config["model"].get("fm_n_factors", 8)` 같은 flat 접두사
방식은 쓰지 않습니다. 마이그레이션 시 `config.yaml`, `train.py`의 모든
`config["model"][...]` 참조, 관련 테스트를 함께 고칩니다(이번 PR 범위 아님).

### 전처리 공유

one-hot 인코딩 + UNKNOWN fallback + 수치형 표준화 로직은 두 번째 모델이 실제로
필요해지는 시점에 `src/models/preprocessing.py` 같은 공용 모듈로 추출합니다.
지금은 추출 대상 구현체가 `LGBMModel` 하나뿐이라(트리 모델이라 이 전처리가
필요 없음) 추출할 코드 자체가 없습니다.

## 범위 밖

- `FMModel`/`MLPModel` 포팅 — `worktree-agent-capability-probe`에 참고용으로 남김
- `config.yaml` 하이퍼파라미터 실제 마이그레이션
- `train.py`의 `model.type` 분기 로직(모델 선택 스위치) — 두 번째 모델이 없어 YAGNI
- 공용 전처리 유틸 실제 추출
- `model_loader.py`/서빙/ONNX 변환

## 완료 조건 (이 문서 기준, #424 완료 조건과 동기화)

- [x] `CTRModel` 인터페이스가 main에 존재하고 `LGBMModel`이 이를 구현한다(기존
      저장 아티팩트 포맷 불변)
- [x] 저장 포맷↔인터페이스 계약을 검증하는 자동 테스트가 있다
- [x] 하이퍼파라미터 중첩 스키마 컨벤션이 이 설계 문서로 남는다(실제 마이그레이션은
      다음 모델 추가 시)
- [x] 공용 전처리 유틸의 설계 가이드가 이 문서로 남는다(실제 추출은 다음 모델 추가 시)
- [x] `FMModel`/`MLPModel`은 이 범위에서 명시적으로 제외되고, `worktree-agent-capability-probe`
      브랜치에 참고용으로만 남는다

## 관련

- #424 — 이 문서가 좁힌 범위대로 완료 조건을 갱신함
- 참고 코드: 로컬 브랜치 `worktree-agent-capability-probe`의 `src/models/base.py`,
  `lgbm_model.py`, `fm_model.py`, `mlp_model.py`, `src/pipeline/train.py`의
  `model.type` 분기 (그대로 병합하지 않고 설계 참고용으로만 사용)
