# MLflow + MinIO 로컬 개발 환경

## 개요

MLflow artifact store를 MinIO (S3 호환)에 연결합니다. MLflow 서버가 S3 API 요청을 MinIO에 중개하므로 (Proxy Mode), 클라이언트에서 AWS credential이나 S3 endpoint URL을 설정할 필요가 없습니다.

### 아키텍처

```
MLflow Client (MLFLOW_TRACKING_URI만 설정, AWS credential 불필요)
    ↓ REST API
MLflow Server (Proxy Mode, S3 credential 보유)
    ↓ S3 API
MinIO (http://minio:9000)
    ↓
mlflow-artifacts/ (bucket)
```

---

## 빠른 시작

### 1. 환경 설정

```bash
cd deploy/mlflow/local

# 템플릿에서 실제 환경파일 생성
cp .env.minio.example .env.minio

# 필요시 비밀번호 변경
# nano .env.minio
```

### 2. 서비스 시작

```bash
# MinIO 환경 (두 파일 함께)
docker compose --env-file .env.minio -f compose.yaml -f compose.minio.yaml up -d
```

### 3. 확인

```bash
# 서비스 상태
docker compose --env-file .env.minio -f compose.yaml -f compose.minio.yaml ps

# MLflow UI
# http://localhost:5000

# MinIO Console
# http://localhost:9001
# 로그인: MINIO_ROOT_USER / MINIO_ROOT_PASSWORD (.env.minio 참조)
```

---

## 환경 변수 (.env.minio)

| 변수 | 기본값 | 설명 |
|------|-------|------|
| MINIO_ROOT_USER | minio-user | MinIO 루트 사용자 |
| MINIO_ROOT_PASSWORD | minio-password-change-me | MinIO 루트 비밀번호 |
| MINIO_API_PORT | 9000 | MinIO API 포트 (호스트) |
| MINIO_CONSOLE_PORT | 9001 | MinIO Console 포트 |
| POSTGRES_USER | mlflow | PostgreSQL 사용자 |
| POSTGRES_PASSWORD | mlflow_local_password | PostgreSQL 비밀번호 |
| POSTGRES_DB | mlflow | PostgreSQL 데이터베이스 |

**중요:** AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, MLFLOW_S3_ENDPOINT_URL는 설정하지 마세요.  
MLflow 서버가 .env.minio에서 자동으로 읽습니다.

---

## Client 설정 (Proxy Mode 검증)

```bash
# AWS credential 제거
unset AWS_ACCESS_KEY_ID
unset AWS_SECRET_ACCESS_KEY
unset AWS_DEFAULT_REGION

# S3 endpoint 제거
unset MLFLOW_S3_ENDPOINT_URL

# MLflow tracking URI만 설정
export MLFLOW_TRACKING_URI=http://localhost:5000

# 이제 artifact 업로드/다운로드 가능 (MLflow 서버가 S3 통신 담당)
```

---

## 서비스 중지

```bash
# 서비스만 중지하고 PostgreSQL·MinIO 데이터 유지
docker compose --env-file .env.minio -f compose.yaml -f compose.minio.yaml down

# 서비스 및 모든 volume 삭제 (전체 초기화)
docker compose --env-file .env.minio -f compose.yaml -f compose.minio.yaml down -v
```

---

## MinIO Console에서 artifact 확인

1. http://localhost:9001 접속
2. MINIO_ROOT_USER / MINIO_ROOT_PASSWORD로 로그인
3. "mlflow-artifacts" bucket 선택
4. "artifacts/" 폴더 내 run별 파일 확인

---

## 문제 해결

### "Service unhealthy"

```bash
docker compose --env-file .env.minio -f compose.yaml -f compose.minio.yaml logs minio
docker compose --env-file .env.minio -f compose.yaml -f compose.minio.yaml logs minio-init
```

### Artifact 업로드 실패

```bash
# 환경 변수 확인
echo "MLFLOW_TRACKING_URI: $MLFLOW_TRACKING_URI"
echo "AWS credential: ${AWS_ACCESS_KEY_ID:-(unset)}"

# MLflow 로그
docker compose --env-file .env.minio -f compose.yaml -f compose.minio.yaml logs mlflow | grep -i s3
```

### MinIO Console 접속 불가

```bash
# MinIO 포트 확인
docker compose --env-file .env.minio -f compose.yaml -f compose.minio.yaml port minio
```

---

## Local File vs MinIO

| 항목 | Local File | MinIO |
|------|-----------|-------|
| 시작 명령 | `docker compose -f compose.yaml up -d` | `docker compose --env-file .env.minio -f compose.yaml -f compose.minio.yaml up -d` |
| 저장소 | 로컬 파일시스템 (/mlartifacts) | S3 호환 객체 저장소 |
| Client 설정 | MLFLOW_TRACKING_URI만 필요 (AWS credential 불필요) | MLFLOW_TRACKING_URI만 필요 (AWS credential 불필요) |
| Proxy Mode | ✗ | ✓ |
| 환경변수 | 기본값 (또는 기존 .env) | .env.minio |

---

## 정리 및 복구

```bash
# 서비스만 중지하고 PostgreSQL·MinIO 데이터 유지
docker compose --env-file .env.minio -f compose.yaml -f compose.minio.yaml down

# 서비스 및 모든 데이터 삭제
docker compose --env-file .env.minio -f compose.yaml -f compose.minio.yaml down -v

# 기존 Local File 환경으로 돌아가기
docker compose -f compose.yaml up -d
```
