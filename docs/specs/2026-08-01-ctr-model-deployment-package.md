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

### 1. 배포 선택 단위

MLflow registered model version이 참조하는 단일 run을 논리적 배포 선택 단위로 삼는다.
MLflow artifact 저장 자체가 트랜잭션으로 원자적이라고 가정하지 않는다. 모델 버전은 아래의
모든 아티팩트와 manifest가 기록되고 자체 검증된 후에만 생성한다.
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
  "sampling_rate": 0.1,
  "artifacts": {
    "model_onnx": {
      "path": "model_onnx",
      "entrypoint": "model.onnx",
      "sha256": "<64 lowercase hex>"
    },
    "feature_columns": {
      "path": "features/feature_columns.json",
      "sha256": "<64 lowercase hex>"
    },
    "categorical_columns": {
      "path": "features/categorical_columns.json",
      "sha256": "<64 lowercase hex>"
    },
    "calibration": {
      "path": "calibration/calibration.json",
      "sha256": "<64 lowercase hex>"
    }
  }
}
```

`sampling_rate`는 manifest에 기록된 값을 유일한 배포 계약 값으로 사용한다. MLflow
parameter/tag나 서버 환경 변수로 calibration 필요 여부를 다시 결정하지 않는다.
`0 < sampling_rate <= 1`인 유한수만 허용한다. `sampling_rate < 1.0`이면 calibration
항목은 객체여야 하고, `sampling_rate == 1.0`이면 반드시 `null`이어야 한다. calibration
JSON 안의 값도 manifest의 `sampling_rate`와 같아야 한다.

`model_onnx`는 MLflow ONNX flavor가 생성하는 디렉터리이며 엔트리 파일은
`model_onnx/model.onnx`으로 고정한다. manifest의 `entrypoint`도 정확히 `model.onnx`여야
한다. 로더가 파일을 탐색하거나 여러 `.onnx` 파일 중 하나를 임의 선택하지 않는다. 디렉터리
전체를 해시하므로 `MLmodel`, 환경 파일, 추가 ONNX 외부 데이터 등 일반 파일이 추가·삭제·변경돼도
검증이 실패한다.

### 3. canonical SHA-256

JSON 파일 아티팩트는 파일 바이트를 그대로 SHA-256으로 계산한다. 디렉터리 아티팩트는 다음
알고리즘을 정확히 사용한다.

1. 대상 아래의 일반 파일만 재귀적으로 열거한다. 숨김 파일과 MLflow 메타파일도 일반 파일이면
   포함한다.
2. 심볼릭 링크, junction 및 기타 reparse point는 파일·디렉터리 모두 거부한다.
3. 각 상대 경로는 원래 대소문자를 보존하고 구분자를 `/`로 정규화한 뒤 UTF-8로 인코딩한다.
4. 상대 경로의 UTF-8 바이트를 기준으로 오름차순 정렬한다.
5. 각 파일에 대해 `uint64_be(path_byte_length)`, `path_utf8_bytes`,
   `uint64_be(file_byte_length)`, `file_bytes`를 순서대로 SHA-256 입력에 넣는다.
6. 권한, 소유자, mtime 및 빈 디렉터리는 포함하지 않는다. 일반 파일이 하나도 없는 디렉터리는
   거부한다.

이 알고리즘은 경로 구분자와 파일시스템 열거 순서의 영향을 제거한다. 파일명만 바뀌거나 manifest가
가리키지 않은 일반 파일이 추가돼도 해시가 바뀐다.

### 4. manifest 스키마와 경로 안전성

manifest와 모든 중첩 pydantic 모델은 `ConfigDict(extra="forbid")`를 사용한다. `sha256`은
정확히 소문자 64자리 hexadecimal이어야 한다. 아티팩트 key는 `model_onnx`,
`feature_columns`, `categorical_columns`, `calibration`만 허용하며 앞의 세 key는 누락하거나
`null`로 둘 수 없다.

모든 `path`와 `entrypoint`는 POSIX 구분자(`/`)를 쓰는 정규화된 상대 경로만 허용한다.
빈 경로, `.`, `..` segment, 절대 경로, 백슬래시, drive prefix, URL 형식은 거부한다. 각
아티팩트의 path와 ONNX entrypoint는 위 표·예시의 고정값과 정확히 같아야 한다.

### 5. 학습 fail-closed

학습은 ONNX 모델을 프로세스 전용 staging 디렉터리에 `mlflow.onnx.save_model`로 먼저 저장하고,
JSON 아티팩트와 함께 해시·manifest를 생성한다. manifest를 다시 로드해 스키마와 모든 로컬
해시를 자체 검증한 뒤 각 아티팩트를 run에 기록한다. 이 전체 과정이 성공한 후에만
`create_model_version`에 해당하는 기존 `register_pending_model()` 경로를 호출한다.
manifest 생성 이전에는 registered model version을 생성하지 않는다.

ONNX 변환·기록 또는 manifest 생성·검증·기록이 실패하면 모델 등록 전에 학습 run을 실패시킨다.
불완전한 run은 원인 분석과 감사 추적을 위해 MLflow의 FAILED run으로 보존하되 registered model
version은 만들지 않는다.
joblib 파일은 로컬 평가·연구 인터페이스 호환을 위해 학습 산출물로 남길 수 있지만 배포
manifest에 포함하지 않으며, 서빙은 이를 다운로드하거나 역직렬화하지 않는다.

### 6. 서빙 fail-closed와 TOCTOU 경계

MLflow/Registry 서빙은 model version의 `run_id` 하나만 다운로드 좌표로 사용한다. manifest와
필수 아티팩트를 프로세스 전용 임시 디렉터리로 복사하고 공유·재사용 MLflow 캐시 경로를 직접
검증 대상으로 쓰지 않는다. 임시 디렉터리 안에서 manifest를 먼저 검증한 뒤 동일한 파일들의
해시를 확인하고, 검증한 바로 그 `model_onnx/model.onnx`으로 즉시 ONNX 세션을 생성한다.
세션과 메타데이터 조립이 끝날 때까지 임시 디렉터리를 외부에 노출하거나 수정하지 않는다.

계약 버전, FeatureService 이름, 필수·조건부 아티팩트 목록 및 모든 해시가 일치한 뒤에만 ONNX 세션과
`Reranker`를 만든다. 다음 경우 `ModelArtifactError`로 기동을 중단한다.

- manifest 또는 ONNX 아티팩트가 없거나 읽을 수 없음
- 알 수 없는 계약 버전 또는 FeatureService 이름
- 경로가 고정된 허용 경로와 다름
- 파일·디렉터리 해시 불일치
- sampling rate와 calibration 존재 조건 불일치
- ONNX Runtime 세션 생성 실패
- model version의 run ID와 실제 아티팩트 다운로드에 사용한 run ID 불일치
- ONNX 입력·출력 계약 불일치

로컬 서빙도 명시적인 `.onnx` 경로를 필수로 받고 동일한 ONNX 추론 경로를 쓴다.
`RERANK_MODEL_PATH`와 joblib 기반 `model_path` 설정은 제거한다.

ONNX 기동 검증은 입력이 정확히 하나이며 이름이 변환 계약의 `input`, dtype이 `tensor(float)`,
shape가 `[None, len(feature_columns)]`인지 확인한다. 출력에는 shape `[None, 2]`인 float 확률
tensor가 정확히 하나 있어야 한다. 동적 batch 차원의 구체적 심볼 이름은 제한하지 않는다.

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

SHA-256 manifest는 우발적 손상과 manifest-artifact 불일치를 검출한다. MLflow 저장소 쓰기
권한을 가진 공격자가 아티팩트와 manifest를 함께 바꾸는 경우의 authenticity는 보장하지 않는다.

## 검증

- ONNX 변환 결과와 LightGBM 예측값이 기존 허용 오차 `atol=1e-4` 이내로 동일하다.
- manifest 생성·저장·로드 round-trip과 결정론적 디렉터리 해시를 검증한다.
- 각 필수 아티팩트의 누락·변조·경로 변경·계약 버전 불일치를 개별 테스트한다.
- manifest의 추가 필드, 미지원 key, 대문자·길이 부족·non-hex 해시를 거부한다.
- `..`, 절대 경로, 백슬래시, URL 경로와 ONNX 디렉터리의 링크를 거부한다.
- 같은 파일 구성의 디렉터리 해시가 Windows/Linux 경로 표현과 무관하게 같음을 검증한다.
- 파일명 변경 및 manifest에 따로 열거되지 않은 추가 파일로 해시가 달라짐을 검증한다.
- `sampling_rate`에 따른 calibration 필수/금지 조건을 테스트한다.
- `sampling_rate <= 0`, `sampling_rate > 1`, NaN과 calibration JSON 불일치를 거부한다.
- MLflow와 Registry 로더가 ONNX 부재·손상 시 joblib으로 폴백하지 않고 실패함을 테스트한다.
- ONNX 입력 이름·dtype·feature 개수와 출력 dtype·shape 불일치를 기동 시 거부한다.
- model version의 run ID와 다운로드 대상 run ID가 동일한지 테스트한다.
- 저장소 전체에서 서빙 런타임의 `joblib.load`와 pickle 메타데이터 경로가 없음을 검사한다.
- 전체 pytest, Ruff, `git diff --check`, serving 이미지 빌드로 회귀를 검증한다.

## 비범위

- 학습·평가용 `CTRModel.save/load` joblib 인터페이스 제거
- calibration 수식을 ONNX 그래프 내부로 병합
- MLflow 저장소 자체의 서명·KMS 기반 공급망 검증
- Airflow DAG·배포 인프라 변경
