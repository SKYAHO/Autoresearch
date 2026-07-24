# Feast Feature Store - GCP 설정 가이드

> TEMP_FEAST_BOOTSTRAP:
> 실제 데이터 적재 파이프라인 완료 전 Feast 스키마/조회 검증을 위한 임시 더미 적재 절차를 포함합니다.
> 실제 BigQuery 적재 파이프라인과 스키마가 확정되면 이 문서는 실제 데이터 기준으로 교체합니다.

BigQuery(Offline Store) + Memorystore for Redis(Online Store) 연동 가이드입니다.

각 단계는 **GCP Console(Web UI)** 기준으로 설명하며, 참고용 **CLI 명령어**도 함께 기재합니다.

## 버전

`feast[gcp]==0.64.0` (`pyproject.toml`의 `feast` 그룹)을 사용합니다. 2026-06 기준
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
export GCP_PROJECT=your-gcp-project-id    # 팀/본인 GCP 프로젝트 ID로 변경
export BQ_LOCATION="asia-northeast3"
```

### Python 가상환경

> **주의**: 시스템 Python 3.14는 Feast 호환성 문제가 있을 수 있습니다. Python 3.12 권장.

```bash
# feast 그룹은 dev/proxy(fastapi<0.129)와 starlette 버전이 충돌하므로
# pyproject.toml 에서 격리 그룹으로 선언되어 있습니다.
uv sync --only-group feast
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
2. 버킷 이름: registry용 버킷 (전역 유일해야 함)
3. 위치: `asia-northeast3`
4. 만들기 클릭

### 3-2. Staging용 (materialize 임시 파일)

**Console**

1. **Cloud Storage** → **버킷** → **만들기**
2. 버킷 이름: staging용 버킷 (전역 유일해야 함)
3. 위치: `asia-northeast3`
4. 만들기 클릭

### CLI (참고)

```bash
# 버킷 이름은 전역 유일해야 하므로 프로젝트에 맞는 이름으로 지정
gsutil mb -l ${BQ_LOCATION} gs://<registry-bucket>/
gsutil mb -l ${BQ_LOCATION} gs://<staging-bucket>/
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
GCP_PROJECT_ID=your-gcp-project-id        # 팀/본인 GCP 프로젝트 ID
BQ_DATASET=feast_offline_store
GCS_REGISTRY_PATH=gs://<registry-bucket>/registry.db
GCS_STAGING_LOCATION=gs://<staging-bucket>/
REDIS_HOST=localhost                      # 터널링 시 localhost
REDIS_PORT=6379
```

### feature_store.yaml (수정 불필요)

`feature_repo/feature_store.yaml`은 위 환경 변수를 `${...}`로 참조하므로
직접 수정하지 않습니다. `.env`만 채우면 됩니다. (`GCS_REGISTRY_PATH`,
`GCS_STAGING_LOCATION`, `GCP_PROJECT_ID`, `BQ_DATASET`, `REDIS_HOST`,
`REDIS_PORT`가 로드 시 주입됩니다.)

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

> Registry가 `GCS_REGISTRY_PATH`로 지정한 GCS 버킷에 저장됩니다.

> **운영 경로 (GHA, 정본)**: `feature_repo/feature_definitions.py`,
> `feature_repo/feature_store.yaml`, `feature_repo/.feastignore`,
> `feature_repo/redis_iam.py`, `pyproject.toml`, `uv.lock`,
> `.github/workflows/feast-apply.yml` 중 하나가 `main`에 merge되면
> `.github/workflows/feast-apply.yml` 워크플로우가 feast 공식 CLI(`feast
> apply`)로 GCS registry를 자동 갱신합니다. `workflow_dispatch`로 수동
> 실행도 가능합니다. feast 0.64의 apply 커맨드는 인증 실패(
> `FeastProviderLoginError`)를 삼키고 exit 0으로 끝나는 결함이 있어, 이
> 워크플로우는 apply 로그의 실패 패턴 grep과 apply 전후 registry generation
> 비교로 침묵 실패를 감지합니다.
>
> **DAG 소비용 경로 (과도기, 폐기 예정)**: Airflow `feast_online_store_materialize`
> DAG는 여전히 공개 batch 명령 `python -m autoresearch.jobs.feast_apply`로
> materialize 직전에 apply를 실행합니다. 이 래퍼는 DAG 소비 전용으로
> 도입되었으며, DAG의 `apply_feature_registry` 태스크가 제거되는 후속
> 이슈에서 함께 삭제될 예정입니다(과도기에는 GHA 경로와 병존). 인자 계약은
> `docs/specs/2026-07-13-public-batch-execution-contract.md`를 참조하세요.

### `full_scan_for_deletion: false` (공유 설정)

`feature_store.yaml`의 `online_store.full_scan_for_deletion: false`는 GHA
apply 경로뿐 아니라 Airflow DAG의 apply 경로에도 함께 적용되는 **공유
설정**입니다. FeatureView 정의를 삭제하는 merge가 발생해도 apply가 Redis에
대해 full-scan 삭제를 시도하지 않으므로, 삭제된 FeatureView의 Redis 키가
자동으로 정리되지 않습니다. 필요 시 수동으로 고아 키를 정리하세요.

### GitHub repo variables ↔ Airflow 주입 env 값 일치

GHA `feast-apply` 워크플로우와 Airflow DAG의 apply 태스크는 **동일한
`feature_store.yaml`**을 서로 다른 실행 환경(GitHub repo variables vs.
Airflow가 주입하는 env)에서 채워 넣습니다. 두 값이 어긋나면 registry에 기록된
`BigQuerySource` 테이블 경로 등이 실행할 때마다 서로 다른 값으로 번갈아
덮어써지는 flip-flop이 발생합니다. 아래 변수는 반드시 두 경로에서 동일한
값이어야 합니다.

| 변수 | GHA (repo variable) | Airflow (주입 env) |
|------|---------------------|---------------------|
| `GCP_PROJECT_ID` | `vars.GCP_PROJECT_ID` | 동일 값 |
| `BQ_DATASET` | `vars.BQ_DATASET` | 동일 값 |
| `GCS_REGISTRY_PATH` | `vars.GCS_REGISTRY_PATH` | 동일 값 |
| `GCS_STAGING_LOCATION` | `vars.GCS_STAGING_LOCATION` | 동일 값 |
| `REDIS_HOST` | `vars.REDIS_HOST` | 동일 값 |
| `REDIS_PORT` | `vars.REDIS_PORT` | 동일 값 |
| `REDIS_TLS_CA_PATH` | 미설정 (GHA 러너는 private Redis 미접근) | Airflow 쪽 값(설정 시) |

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

## GCP 계정 전환 (개인 → 팀 프로젝트)

환경별 값은 모두 `.env`로 주입하므로, 코드나 `feature_store.yaml`은
수정하지 않습니다. 전환 절차:

1. 팀 GCP 프로젝트에 BigQuery 데이터셋, GCS 버킷(registry/staging), (서빙 시)
   Redis 준비 — 전부 `asia-northeast3` 리전
2. `.env`의 값 교체: `GCP_PROJECT_ID`, `BQ_DATASET`, `GCS_REGISTRY_PATH`,
   `GCS_STAGING_LOCATION`, `REDIS_HOST` 등
3. 인증 설정 (아래 "인증 방식" 참고)
4. `feast apply && feast materialize-incremental ...` 재실행

### 인증 방식

로컬에서 팀 프로젝트에 접근하는 방법은 두 가지이며, 팀 정책에 따릅니다.

- **개인 계정 ADC**: `gcloud auth application-default login` 실행. 키 파일이
  없어 유출 위험이 낮아 로컬 개발에 권장. 인프라 담당이 내 계정에 역할 부여.
- **서비스 계정 키(JSON)**: 발급받은 키를 `keys/`에 두고 `.env`의
  `GOOGLE_APPLICATION_CREDENTIALS`로 지정. CI·자동화에서 사용. **키 파일은
  시크릿이므로 커밋 금지** (`keys/`, `.gcp-creds.json`은 `.gitignore` 처리됨).

필요 IAM 역할 (offline store 기준): BigQuery `dataEditor` + `jobUser`,
GCS(registry·staging 버킷) `storage.objectAdmin`.

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
