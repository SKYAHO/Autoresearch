# Feast Feature Store - GCP 설정 가이드

BigQuery(Offline Store) + Memorystore for Redis(Online Store) 연동 가이드입니다.

---

## 0. 사전 준비

### gcloud CLI 설치 (macOS)

```bash
brew install --cask google-cloud-sdk
gcloud init
gcloud auth login
gcloud auth application-default login   # Feast 인증용
```

### 공통 환경 변수

이후 모든 단계에서 사용할 변수입니다. 터미널 세션에서 한 번 실행하세요:

```bash
export GCP_PROJECT=$(gcloud config get-value project)
export BQ_LOCATION="asia-northeast3"
```

### Python 가상환경 (Feast 의존성 충돌 방지)

> **주의**: 시스템 Python 3.14는 Feast 호환성 문제가 있을 수 있습니다.
> Python 3.11~3.12 권장.

```bash
# pyenv로 Python 3.12 설치 권장
pyenv install 3.12
pyenv local 3.12

python -m venv .venv
source .venv/bin/activate
pip install -r requirements-feast.txt
```

---

## 1. GCP 서비스 계정 생성

```bash
# 서비스 계정 생성
gcloud iam service-accounts create feast-sa \
  --display-name="Feast Feature Store SA"

# 권한 부여 (BigQuery, GCS, Redis)
SA_EMAIL="feast-sa@${GCP_PROJECT}.iam.gserviceaccount.com"

gcloud projects add-iam-policy-binding $GCP_PROJECT \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/bigquery.admin"

gcloud projects add-iam-policy-binding $GCP_PROJECT \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/storage.admin"

gcloud projects add-iam-policy-binding $GCP_PROJECT \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/redis.editor"

# 키 다운로드 (커밋 금지! .gitignore에 이미 포함됨)
mkdir -p keys
gcloud iam service-accounts keys create keys/service-account.json \
  --iam-account=${SA_EMAIL}
```

```bash
# 서비스 계정 키 경로 설정
export GOOGLE_APPLICATION_CREDENTIALS="./keys/service-account.json"
```

---

## 2. BigQuery 데이터셋 생성

```bash
bq --location=${BQ_LOCATION} mk \
  --dataset \
  --description "Feast Offline Store" \
  ${GCP_PROJECT}:feast_offline_store
```

---

## 3. GCS 버킷 생성 (materialize 임시 저장소)

```bash
gsutil mb -l ${BQ_LOCATION} gs://feast-staging-${GCP_PROJECT}/
```

---

## 4. Memorystore for Redis 인스턴스 생성

```bash
gcloud redis instances create feast-redis \
  --size=1 \
  --tier=basic \
  --region=${BQ_LOCATION} \
  --redis-version=redis_7_0

# 연결 정보 확인
gcloud redis instances describe feast-redis \
  --region=${BQ_LOCATION} \
  --format="value(host,port)"
```

> **주의**: Memorystore는 VPC 내 리소스입니다. 로컬에서 직접 접속하려면
> Serverless VPC Access Connector 또는 GCE/Bastion을 통한 터널링이 필요합니다.
> 빠른 테스트를 원하면 로컬 Docker Redis를 사용하세요:
> ```bash
> docker run -d --name redis -p 6379:6379 redis:7
> ```

---

## 5. 환경 설정 (.env)

```bash
cp .env.example .env
# .env 파일 편집: REDIS_HOST, REDIS_PORT, GCP_PROJECT_ID 등 입력
```

`feature_store/feature_store.yaml`의 다음 항목을 `.env` 값에 맞게 직접 수정하세요:
- `offline_store.project` → GCP 프로젝트 ID
- `online_store.connection_string` → Redis 호스트:포트
- `offline_config.gcs_staging_location` → GCS staging 경로

---

## 6. 더미 데이터 생성 및 BigQuery 업로드

```bash
# 더미 데이터 생성 → BigQuery 테이블 직접 적재
GOOGLE_APPLICATION_CREDENTIALS=./keys/service-account.json \
python scripts/generate_and_upload_dummy_data.py
```

---

## 7. Feast 적용 (Registry 등록)

```bash
cd feature_store
feast apply
```

---

## 8. Materialize (BigQuery -> Redis 동기화)

```bash
# 전체 동기화
feast materialize-incremental $(date -u +"%Y-%m-%dT%H:%M:%S")
```

---

## 9. 검증

```bash
# feature_store/ 디렉토리 안에서 실행 중이므로 프로젝트 루트로 복귀
cd ..
python scripts/test_feature_retrieval.py
```

---

## GCP 계정 전환 (개인 -> 프로젝트)

1. 프로젝트 GCP에서 동일하게 서비스 계정, BigQuery, GCS, Redis 생성
2. `.env`의 `GCP_PROJECT_ID`, `REDIS_HOST` 등 업데이트
3. `feature_store/feature_store.yaml`의 project, connection_string 등 직접 수정
4. `feast apply && feast materialize-incremental ...` 재실행

---

## 문제 해결

| 증상 | 원인 / 해결 |
|------|------------|
| `Permission denied` on BigQuery | 서비스 계정 권한 확인 |
| Redis connection timeout | VPC 방화벽 / Serverless VPC Access 확인 |
| Python 호환성 에러 | Python 3.12 사용 권장 (3.14 미지원) |
| `feast apply` schema error | BigQuery 테이블 컬럼명/타입이 FeatureView schema와 일치하는지 확인 |
