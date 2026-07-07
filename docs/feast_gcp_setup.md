# Feast Feature Store - GCP 설정 가이드

> TEMP_FEAST_BOOTSTRAP:
> 실제 데이터 적재 파이프라인 완료 전 Feast 스키마/조회 검증을 위한 임시 더미 적재 절차를 포함합니다.
> 실제 BigQuery 적재 파이프라인과 스키마가 확정되면 이 문서는 실제 데이터 기준으로 교체합니다.

BigQuery(Offline Store) + Memorystore for Redis(Online Store) 연동 가이드입니다.

각 단계는 **GCP Console(Web UI)** 기준으로 설명하며, 참고용 **CLI 명령어**도 함께 기재합니다.

## 버전

`feast[gcp]==0.64.0` (`requirements-feast.txt`)을 사용합니다. 2026-06 기준
최신 릴리스로, 선택 근거는 다음과 같습니다:

- `entity_key_serialization_version: 2`가 Feast 0.50에서 제거되어, 실데이터
  운영 전에 v3로 올려두어야 이후 online store 재적재 비용이 발생하지 않습니다.
- 최신 버전은 Python `>=3.10`을 지원해 구버전(0.40.x)의 Python 호환 제약이
  완화되었습니다.

버전을 올릴 때는 `feature_store.yaml` 설정 형식과 사용 중인 API의 릴리스
노트를 확인하고, `feast apply` → 조회 검증을 다시 수행합니다.

---

## 진행 체크리스트

- [ ] 0. 사전 준비 (gcloud CLI, Python)
- [ ] 1. 서비스 계정 생성
- [ ] 2. BigQuery 데이터셋 생성
- [ ] 3. GCS 버킷 생성 (Registry용, Staging용)
- [ ] 4. Memorystore for Redis 인스턴스 생성
- [ ] 5. Bastion (GCE) 인스턴스 생성 — Redis 터널링용
- [ ] 6. 로컬 환경 설정 (.env, feature_store.yaml)
- [ ] 7. 더미 데이터 BigQuery 업로드
- [ ] 8. Feast 적용 (feast apply)
- [ ] 9. Materialize (BigQuery → Redis 동기화)
- [ ] 10. Feature 조회 검증

---

## 0. 사전 준비

### gcloud CLI 설치 (macOS)

```bash
brew install --cask google-cloud-sdk
gcloud init                    # 프로젝트 설정
gcloud auth login              # 계정 인증
gcloud auth application-default login   # Feast 인증용
```

> 설치 확인: https://console.cloud.google.com/ 에서 프로젝트 ID 확인

### 공통 환경 변수

터미널 세션에서 한 번 실행 (이후 모든 단계에서 사용):

```bash
export GCP_PROJECT=autoresearch-skyaho    # 본인 GCP 프로젝트 ID로 변경
export BQ_LOCATION="asia-northeast3"
```

### Python 가상환경

> **주의**: 시스템 Python 3.14는 Feast 호환성 문제가 있을 수 있습니다. Python 3.12 권장.

```bash
pyenv install 3.12
pyenv local 3.12

python -m venv .venv
source .venv/bin/activate
pip install -r requirements-feast.txt
```

---

## 1. 서비스 계정 생성

Feast가 BigQuery, GCS, Redis에 접근하기 위한 전용 계정입니다.

### Console

1. **IAM 및 관리자** → **서비스 계정** → **서비스 계정 만들기**
2. 서비스 계정 이름: `feast-sa`
3. 역할 부여:
   - **BigQuery** → **BigQuery Admin**
   - **Cloud Storage** → **Storage Admin**
   - **Memorystore** → **Redis Editor**
4. 완료 후 **키** 탭 → **키 추가** → **새 키 만들기** → JSON → 다운로드
5. 다운로드한 파일을 프로젝트 `keys/service-account.json`으로 이동

### CLI (참고)

```bash
# 서비스 계정 생성
gcloud iam service-accounts create feast-sa \
  --display-name="Feast Feature Store SA"

# 권한 부여
SA_EMAIL="feast-sa@${GCP_PROJECT}.iam.gserviceaccount.com"

gcloud projects add-iam-policy-binding $GCP_PROJECT \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/bigquery.admin"

gcloud projects add-iam-policy-binding $GCP_PROJECT \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/storage.admin"

gcloud projects add-iam-policy-binding $GCP_PROJECT \
  --member="serviceAccount:${SA_EMAIL}" --role="roles/redis.editor"

# 키 다운로드
mkdir -p keys
gcloud iam service-accounts keys create keys/service-account.json \
  --iam-account=${SA_EMAIL}
```

### 확인

```bash
export GOOGLE_APPLICATION_CREDENTIALS="./keys/service-account.json"
echo $GOOGLE_APPLICATION_CREDENTIALS   # 경로 출력 확인
```

- [ ] 완료

---

## 2. BigQuery 데이터셋 생성

Feature 원본 데이터를 저장할 데이터셋입니다.

### Console

1. **BigQuery** → **탐색기** → 프로젝트 ID 옆 **...** → **데이터셋 만들기**
2. 데이터셋 ID: `feast_offline_store`
3. 위치: `asia-northeast3`
4. **데이터셋 만들기** 클릭

### CLI (참고)

```bash
bq --location=${BQ_LOCATION} mk \
  --dataset \
  --description "Feast Offline Store" \
  ${GCP_PROJECT}:feast_offline_store
```

- [ ] 완료

---

## 3. GCS 버킷 생성 (2개)

Feast에서 두 가지 용도의 버킷이 필요합니다.

### 3-1. Registry용 (FeatureView 메타데이터 저장)

**Console**

1. **Cloud Storage** → **버킷** → **만들기**
2. 버킷 이름: `feast-registry-autoresearch`
3. 위치: `asia-northeast3`
4. 만들기 클릭

### 3-2. Staging용 (materialize 임시 파일)

**Console**

1. **Cloud Storage** → **버킷** → **만들기**
2. 버킷 이름: `feast-staging-autoresearch`
3. 위치: `asia-northeast3`
4. 만들기 클릭

### CLI (참고)

```bash
gsutil mb -l ${BQ_LOCATION} gs://feast-registry-autoresearch/
gsutil mb -l ${BQ_LOCATION} gs://feast-staging-autoresearch/
```

- [ ] Registry 버킷 완료
- [ ] Staging 버킷 완료

---

## 4. Memorystore for Redis 인스턴스 생성

실시간 Feature 조회용 Online Store입니다.

### Console

1. **Memorystore** → **Redis** → **인스턴스 만들기**
2. 인스턴스 ID: `feast-redis`
3. 티어: **Basic** (개발용, 단일 노드)
4. 용량: **1 GiB**
5. 리전: `asia-northeast3`
6. Redis 버전: **7.x**
7. **만들기** 클릭 (생성에 약 2~3분 소요)
8. 생성 완료 후 **IP 주소**와 **포트** 확인

### CLI (참고)

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

> 생성된 IP 주소와 포트를 메모해 두세요 (6단계에서 사용).

- [ ] 완료
- [ ] Redis IP 주소: `_____________`
- [ ] Redis 포트: `_____________`

---

## 5. Bastion (GCE) 인스턴스 생성 — Redis 터널링용

Memorystore는 VPC 내부 리소스라 로컬에서 직접 접근할 수 없습니다.
GCE 인스턴스를 통해 SSH 터널링으로 접근합니다.

### Console

1. **Compute Engine** → **VM 인스턴스** → **인스턴스 만들기**
2. 이름: `feast-bastion`
3. 리전: `asia-northeast3`
4. 머신 유형: **e2-micro** (가장 저렴)
5. 부팅 디스크: **Debian GNU/Linux 12**
6. **네트워크** 설정 확장:
   - 네트워크 인터페이스: **default** (Memorystore와 같은 VPC)
7. **만들기** 클릭

### CLI (참고)

```bash
gcloud compute instances create feast-bastion \
  --machine-type=e2-micro \
  --zone=asia-northeast3-a \
  --image-family=debian-12 \
  --image-project=debian-cloud
```

### SSH 터널링 설정

Memorystore Redis IP가 `10.x.x.x` 라면:

```bash
# 터미널 1: 터널 유지 (켜둔 상태로 유지)
gcloud compute ssh feast-bastion \
  --zone=asia-northeast3-a \
  -- -L 6379:10.x.x.x:6379

# 이제 localhost:6379 로 Memorystore Redis에 접근 가능
```

- [ ] Bastion 인스턴스 완료
- [ ] SSH 터널링 연결 확인

---

## 6. 로컬 환경 설정

### .env 파일 생성

```bash
cp .env.example .env
```

`.env` 파일을 열어서 다음 값을 수정:

```
GOOGLE_APPLICATION_CREDENTIALS=./keys/service-account.json
GCP_PROJECT_ID=autoresearch-skyaho       # 본인 GCP 프로젝트 ID
BQ_DATASET=feast_offline_store
REDIS_HOST=localhost                      # 터널링 시 localhost
REDIS_PORT=6379
```

### feature_store.yaml 수정

`feature_repo/feature_store.yaml`에서 다음 항목 확인/수정:

```yaml
offline_store:
  project_id: autoresearch-skyaho-501202  # 본인 GCP 프로젝트 ID
  gcs_staging_location: gs://feast-staging-autoresearch/

online_store:
  connection_string: "localhost:6379"     # 터널링 시 localhost
```

- [ ] .env 설정 완료
- [ ] feature_store.yaml 수정 완료

---

## 7. 더미 데이터 BigQuery 업로드

```bash
# SSH 터널링 켜둔 상태에서 실행
GOOGLE_APPLICATION_CREDENTIALS=./keys/service-account.json \
python scripts/generate_and_upload_dummy_data.py
```

BigQuery Console에서 3개 테이블에 데이터가 적재되었는지 확인:
- `user_features`
- `video_features`
- `user_video_interaction`

- [ ] 완료

---

## 8. Feast 적용 (Registry 등록)

```bash
cd feature_repo
feast apply
```

> Registry가 GCS 버킷(`gs://feast-registry-autoresearch/`)에 저장됩니다.

- [ ] 완료

---

## 9. Materialize (BigQuery → Redis 동기화)

```bash
# feature_repo/ 디렉토리 안에서 실행
feast materialize-incremental $(date -u +"%Y-%m-%dT%H:%M:%S")
```

> BigQuery 데이터를 읽어 GCS Staging을 거쳐 Redis에 적재합니다.

- [ ] 완료

---

## 10. Feature 조회 검증

```bash
# feature_repo/ 디렉토리 안이므로 프로젝트 루트로 복귀
cd ..
python scripts/verify_feature_retrieval.py
```

성공 시 Online / Historical Feature 조회 결과가 출력됩니다.

- [ ] Online Feature 조회 성공
- [ ] Historical Feature 조회 성공

---

## GCP 계정 전환 (개인 → 프로젝트)

1. 프로젝트 GCP에서 동일하게 서비스 계정, BigQuery, GCS, Redis, Bastion 생성
2. `.env`의 `GCP_PROJECT_ID`, `REDIS_HOST` 등 업데이트
3. `feature_repo/feature_store.yaml`의 project_id, connection_string, 버킷 이름 직접 수정
4. `feast apply && feast materialize-incremental ...` 재실행

---

## 문제 해결

| 증상 | 원인 / 해결 |
|------|------------|
| `Permission denied` on BigQuery | 서비스 계정 권한 확인 (BigQuery Admin) |
| `Permission denied` on GCS | 서비스 계정 권한 확인 (Storage Admin) |
| Redis connection timeout | SSH 터널링 활성화 확인, Bastion과 Redis가 같은 VPC인지 확인 |
| Python 호환성 에러 | Python 3.12 사용 권장 (3.14 미지원) |
| `feast apply` schema error | BigQuery 테이블 컬럼명/타입이 FeatureView schema와 일치하는지 확인 |
| `Registry` 접근 실패 | GCS Registry 버킷 권한 확인 (Storage Admin) |
