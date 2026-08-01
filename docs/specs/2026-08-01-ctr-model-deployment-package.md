# CTR 모델 배포 패키지 완성 (#302)

> 상태: 구현 전 설계 확정
> 작성일: 2026-08-01
> 관련 이슈: #302, #390

## 목적

CTR 서빙이 MLflow run에서 받은 모델·피처 메타데이터·calibration을 하나의 검증된
배포 단위로만 로드하게 한다. pickle 계열 역직렬화를 서빙 경로에서 완전히 제거하고,
필수 아티팩트가 없거나 변조된 경우 서버가 기동하지 않는 fail-closed 계약을 확정한다.

## 현행과 남은 문제

이미 완료된 범위는 다음과 같다.

- LightGBM 모델을 `model_onnx/`에 기록하고 `onnxruntime`으로 추론한다(#336).
- feature/categorical 메타데이터를 JSON으로 기록하고 로드한다(#344).
- calibration을 메인 모델과 같은 MLflow run의 `calibration/calibration.json`에
  종속시켜 alias 간 동기화 race를 구조적으로 제거했다(#390).
- Feast `ctr_training_v1` FeatureService를 학습 피처 계약으로 사용한다(#358).

남은 문제는 ONNX 변환 실패를 학습이 best-effort로 넘기고, 서빙이 ONNX 부재·손상 시
`model/lgbm_model.joblib`을 내려받아 `joblib.load`로 폴백한다는 점이다. 또한 같은 run에
아티팩트가 있다는 사실만으로 파일 내용의 무결성과 계약 버전을 검증하지 않는다.

## 결정

### 1. 배포 단위

MLflow registered model version이 가리키는 단일 run을 원자적 배포 단위로 삼는다.
별도 calibration registered model이나 두 alias를 동시에 이동하는 헬퍼는 만들지 않는다.
메인 alias 한 번의 이동으로 같은 run의 ONNX·feature JSON·categorical JSON·선택적
calibration·manifest가 함께 선택된다.

### 2. 필수 아티팩트와 manifest

학습은 registered model을 생성하기 전에 아래 아티팩트를 완성한다.

| 논리 이름 | MLflow 경로 | 필수 조건 |
|---|---|---|
| main model | `model_onnx/` | 항상 필수 |
| feature columns | `features/feature_columns.json` | 항상 필수 |
| categorical columns | `features/categorical_columns.json` | 항상 필수 |
| calibration | `calibration/calibration.json` | `sampling_rate < 1.0`일 때 필수 |
| package manifest | `manifest/manifest.json` | 항상 필수 |

manifest 계약 버전은 `ctr-model-package-v1`이다. 다음 필드를 포함한다.

```json
{
  "contract_version": "ctr-model-package-v1",
  "feature_service": "ctr_training_v1",
  "artifacts": {
    "model_onnx": {"path": "model_onnx", "sha256": "<64 hex>"},
    "feature_columns": {
      "path": "features/feature_columns.json",
      "sha256": "<64 hex>"
    },
    "categorical_columns": {
      "path": "features/categorical_columns.json",
      "sha256": "<64 hex>"
    },
    "calibration": null
  }
}
```

`model_onnx` 해시는 MLflow가 만든 디렉터리 전체를 대상으로 한다. 상대 경로와 파일 바이트를
정렬된 순서로 해시해 디렉터리 구성과 내용 변경을 모두 검출한다. JSON 파일은 파일 바이트를
그대로 SHA-256으로 계산한다. `sampling_rate < 1.0`이면 calibration 항목은 객체여야 하고,
그 외에는 `null`이어야 한다.

### 3. 학습 fail-closed

ONNX 변환·기록 또는 manifest 생성·기록이 실패하면 모델 등록 전에 학습 run을 실패시킨다.
joblib 파일은 로컬 평가·연구 인터페이스 호환을 위해 학습 산출물로 남길 수 있지만 배포
manifest에 포함하지 않으며, 서빙은 이를 다운로드하거나 역직렬화하지 않는다.

### 4. 서빙 fail-closed

MLflow/Registry 서빙은 manifest를 먼저 내려받고 pydantic 모델로 검증한다. 계약 버전,
FeatureService 이름, 필수·조건부 아티팩트 목록 및 모든 해시가 일치한 뒤에만 ONNX 세션과
`Reranker`를 만든다. 다음 경우 `ModelArtifactError`로 기동을 중단한다.

- manifest 또는 ONNX 아티팩트가 없거나 읽을 수 없음
- 알 수 없는 계약 버전 또는 FeatureService 이름
- 경로가 고정된 허용 경로와 다름
- 파일·디렉터리 해시 불일치
- sampling rate와 calibration 존재 조건 불일치
- ONNX Runtime 세션 생성 실패

로컬 서빙도 명시적인 `.onnx` 경로를 필수로 받고 동일한 ONNX 추론 경로를 쓴다.
`RERANK_MODEL_PATH`와 joblib 기반 `model_path` 설정은 제거한다.

## 구성요소 경계

- `src/tracking/model_package.py`: manifest 스키마, 결정론적 파일/디렉터리 해시,
  생성·저장·검증을 담당한다.
- `src/pipeline/train.py`: 필수 아티팩트를 생성한 뒤 manifest를 기록하고, 완료 후에만
  registered model 후보를 만든다.
- `src/serving/model_loader.py`: manifest와 다운로드 결과를 검증한 뒤 ONNX 기반
  `Reranker`를 조립한다. pickle/joblib 로딩은 담당하지 않는다.
- `src/serving/onnx_model.py`: ONNX 입력 인코딩과 확률 추론만 담당하며 패키지 검증은
  담당하지 않는다.

## 전환과 운영 확인

기존 joblib-only champion은 새 서빙 코드에서 로드되지 않는다. 배포 전 현재 champion이
`model_onnx/`, 두 JSON 메타데이터, 필요 시 calibration을 갖는지 확인하고, 이 변경을 적용한
학습으로 manifest를 가진 새 버전을 생성·승격한 뒤 서빙 이미지를 롤아웃한다. 롤백 대상도
manifest 계약을 충족하는 버전으로 제한한다.

## 검증

- ONNX 변환 결과와 LightGBM 예측값이 기존 허용 오차 `atol=1e-4` 이내로 동일하다.
- manifest 생성·저장·로드 round-trip과 결정론적 디렉터리 해시를 검증한다.
- 각 필수 아티팩트의 누락·변조·경로 변경·계약 버전 불일치를 개별 테스트한다.
- `sampling_rate`에 따른 calibration 필수/금지 조건을 테스트한다.
- MLflow와 Registry 로더가 ONNX 부재·손상 시 joblib으로 폴백하지 않고 실패함을 테스트한다.
- 저장소 전체에서 서빙 런타임의 `joblib.load`와 pickle 메타데이터 경로가 없음을 검사한다.
- 전체 pytest, Ruff, `git diff --check`, serving 이미지 빌드로 회귀를 검증한다.

## 비범위

- 학습·평가용 `CTRModel.save/load` joblib 인터페이스 제거
- calibration 수식을 ONNX 그래프 내부로 병합
- MLflow 저장소 자체의 서명·KMS 기반 공급망 검증
- Airflow DAG·배포 인프라 변경
