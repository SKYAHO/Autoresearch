# CTR 학습 이미지

CTR 학습 파이프라인(`src/`)을 컨테이너에서 실행하기 위한 이미지 정의와 MLflow
연동 방법을 설명합니다.

## 개요

`Dockerfile.app`(배치 CLI, `autoresearch/`)과 `Dockerfile.train`(학습 파이프라인,
`src/`)은 책임이 다른 별도 이미지입니다. 두 이미지 모두 루트 `pyproject.toml`
+ `uv.lock`을 공유하지만, 각각 자신이 필요한 패키지만 담습니다(`Dockerfile.app`은
`autoresearch/`만, `Dockerfile.train`은 `src/`만 COPY).

| 항목 | 값 |
|---|---|
| 베이스 이미지 | `python:3.12-slim` (multi-stage, uv 기반) |
| 진입점 | `python -m src.cli <command>` (Typer CLI: `build-features`, `train-model`, `evaluate-model`, `run-pipeline`) |
| 실행 유저 | non-root (`appuser`) |
| CI 검증 | `.github/workflows/ci.yml`의 `docker-build` job (빌드 + `--help` 스모크 체크) |

## 로컬 빌드

```bash
docker build -f Dockerfile.train --tag autoresearch-training:local .
docker run --rm autoresearch-training:local python -m src.cli train-model --help
```

## MLflow 연동

`src/pipeline/train.py`는 `MLFLOW_TRACKING_URI` 환경변수를 읽고, 없으면
`http://localhost:5000`(로컬 docker-compose MLflow 기준)로 fallback합니다.

```python
tracking_uri = os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5000")
```

실행 환경별로 반드시 명시적으로 값을 주입해야 합니다. 기본값은 로컬 개발용이며
운영 환경에서 그대로 두면 잘못된 주소로 접속을 시도합니다.

| 환경 | `MLFLOW_TRACKING_URI` |
|---|---|
| 로컬 (`deploy/mlflow/local` docker-compose) | `http://localhost:5000` (기본값, 별도 설정 불필요) |
| GKE in-cluster (Airflow KubernetesPodOperator) | `http://mlflow.mlflow:5000` (OAuth2-proxy 미경유, 내부 전용) |

GKE 배포 세부사항(네임스페이스, NetworkPolicy, 접속 방법)은
`Autoresearch-infra/docs/MLFLOW_OPERATIONS_RUNBOOK.md`를 참조하세요. Artifact는
MLflow 서버의 proxy 모드로 기록되므로, 학습 이미지에는 GCS 자격 증명이 필요
없습니다.

## 이미지 배포

`.github/workflows/publish-training-image.yml`(`workflow_dispatch` 수동 트리거)이
`Dockerfile.train`을 빌드해 GAR(`asia-northeast3-docker.pkg.dev/ar-infra-501607/
autoresearch-dev-docker/autoresearch-training`)로 push합니다.
`Autoresearch-airflow`의 `build-and-push.yml`과 동일하게 Workload Identity
Federation(`autoresearch-github` pool)으로 `autoresearch-dev-gar-pusher`
서비스 계정을 impersonate합니다.

이 워크플로가 성공하려면, 해당 서비스 계정의 `roles/iam.workloadIdentityUser`
바인딩이 `SKYAHO/Autoresearch` 저장소를 포함하도록 확장돼 있어야 합니다(기존엔
`SKYAHO/Autoresearch-airflow`만 허용돼 있었음 — 인프라 관리자 조치 필요,
`SKYAHO/Autoresearch#185` 참고).

워크플로 실행 후 Step Summary에 push된 digest가 출력됩니다. 이 digest를
`Autoresearch-airflow`의 `deploy/airflow/values.yaml`(
`AIRFLOW_VAR_AUTORESEARCH_TRAINING_IMAGE`)에 반영해야 `ctr_model_training`
DAG가 이 이미지를 사용합니다 — 자동 반영은 아직 없고 수동 PR로 갱신합니다.

## 관련 이슈

- `SKYAHO/Autoresearch#169` — 학습 이미지 패키징(이 문서)
- `SKYAHO/Autoresearch-infra#234` — NetworkPolicy egress 허용
- `SKYAHO/Autoresearch-airflow#72` — CTR 학습 DAG
- `SKYAHO/Autoresearch#185` — GAR publish 자동화
