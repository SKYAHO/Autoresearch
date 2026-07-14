# 로컬 MLflow 검증 환경

PostgreSQL 백엔드 스토어 및 artifact proxy 방식의 로컬 MLflow 서버 구성입니다.

## 사전 조건

- Docker 및 Docker Compose 설치
- 호스트 Python 환경에 `mlflow==2.22.1` 설치
  ```bash
  pip install mlflow==2.22.1
  ```
  (smoke test가 서버와 클라이언트 MLflow 버전 일치를 확인하므로 필요)

## 기본 모드 (로컬 전용)

로컬 named volume을 artifact store로 사용합니다. GCS 자격증명이 필요하지 않습니다.

### 1. 환경 파일 설정

```bash
cp .env.example .env
```

### 2. 서버 기동

```bash
docker compose up -d --wait
```

`--wait` 플래그는 서비스가 healthcheck를 통과할 때까지 대기합니다.

### 3. Smoke Test 실행

```bash
python ../../../scripts/mlflow_smoke_test.py
```

다음을 검증합니다:
- MLflow 서버 및 버전 확인
- Run 생성, parameter/metric/artifact 기록
- Run 재조회 및 artifact 다운로드 (왕복 검증)
- PostgreSQL 백엔드 저장 확인

### 4. MLflow UI 확인

브라우저에서 `http://localhost:5000`에 접속하여 Run ID와 일치하는 Run을 직접 확인합니다.

### 5. 서버 종료 (named volume 유지)

```bash
docker compose down
```

## 재시작 후 영속성 확인

데이터가 PostgreSQL과 named volume에 저장되므로, 재시작 후에도 Run이 유지됩니다.

### 1. 서버 재시작

```bash
docker compose up -d --wait
```

### 2. 이전 Run 재조회

```bash
python ../../../scripts/mlflow_smoke_test.py --query-only
```

`--query-only` 모드는 새 Run을 생성하지 않고, 이전에 생성한 Run을 태그 기반으로 검색하여 메트릭 및 artifact를 재확인합니다.

## MinIO (S3 호환) 검증 모드 (선택 사항)

로컬 S3 호환 객체 저장소 (MinIO)에 artifact를 업로드할 경우, compose override 방식으로 MinIO를 추가합니다.

### 1. 환경 파일 생성

```bash
cp .env.minio.example .env.minio
# 필요시 비밀번호 변경
# nano .env.minio
```

### 2. MinIO 환경 기동

```bash
docker compose --env-file .env.minio -f compose.yaml -f compose.minio.yaml up -d
```

### 3. MLflow UI 및 MinIO Console 확인

- MLflow UI: http://localhost:5000
- MinIO Console: http://localhost:9001 (user: minio-user)

### 4. MinIO 환경 종료

```bash
# 데이터 유지
docker compose --env-file .env.minio -f compose.yaml -f compose.minio.yaml down

# 전체 삭제
docker compose --env-file .env.minio -f compose.yaml -f compose.minio.yaml down -v
```

상세 가이드는 [README-minio.md](README-minio.md) 참고.

---

## GCS 검증 모드 (선택 사항)

실 GCS 버킷에 artifact를 업로드할 경우, `.env`를 다음과 같이 수정하고 override 파일을 사용합니다.

### 1. 환경 파일 수정

```bash
# .env 파일에서 다음을 변경:
MLFLOW_SMOKE_EXPERIMENT=smoke-test-gcs
MLFLOW_ARTIFACT_STORE_MODE=gcs
MLFLOW_ARTIFACT_DESTINATION=gs://your-bucket/mlflow-artifacts
GCS_KEY_FILE_HOST_PATH=./keys/service-account.json
```

### 2. GCS 서비스 계정 키 배치

`deploy/mlflow/local/keys/service-account.json`에 서비스 계정 키를 저장합니다.

```bash
mkdir -p keys
# service-account.json을 여기에 배치
```

### 3. Compose 파일 검증 및 기동

**반드시 `deploy/mlflow/local/` 디렉토리에서 실행하세요.** Override 파일의 상대 경로가 base compose 파일 기준으로 해석됩니다.

```bash
# 병합 결과 확인 (문법 검사)
docker compose -f compose.yaml -f compose.gcs.yaml config --quiet

# 서버 기동
docker compose -f compose.yaml -f compose.gcs.yaml up -d --wait
```

### 4. Smoke Test 실행

```bash
python ../../../scripts/mlflow_smoke_test.py
```

이 모드에서는 artifact가 GCS 버킷에 저장됩니다.

### 5. 서버 종료

```bash
docker compose -f compose.yaml -f compose.gcs.yaml down
```

## 완전 정리

named volume 포함 모든 데이터를 삭제합니다.

```bash
# 로컬 모드
docker compose down -v

# GCS 모드를 사용했던 경우
docker compose -f compose.yaml -f compose.gcs.yaml down -v
```

## 구현 노트

- MLflow 컨테이너의 `pip install`은 로컬 검증 전용 임시 방식입니다.
  #94(MLflow 커스텀 이미지)에서 Dockerfile 기반으로 대체될 예정입니다.
- 로컬 모드와 GCS 모드는 서로 다른 Experiment를 사용합니다.
  같은 Experiment를 재사용하면 GCS 검증이 기존 로컬 artifact location을 물려받아 무효화됩니다.
- artifact는 `--artifacts-destination` (서버 proxy 모드)를 사용하므로,
  호스트의 smoke test 클라이언트는 artifact store 자격증명이 필요하지 않습니다.
  (GCS 모드에서는 MLflow 서버가 GCS 자격증명을 사용합니다.)
