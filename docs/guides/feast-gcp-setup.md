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
FEAST_ONLINE_FULL_SCAN_FOR_DELETION=true  # 아래 주의 참조 (#399)
```

### feature_store.yaml (수정 불필요)

`feature_repo/feature_store.yaml`은 위 환경 변수를 `${...}`로 참조하므로
직접 수정하지 않습니다. (`GCS_REGISTRY_PATH`, `GCS_STAGING_LOCATION`,
`GCP_PROJECT_ID`, `BQ_DATASET`, `REDIS_HOST`, `REDIS_PORT`,
`FEAST_ONLINE_FULL_SCAN_FOR_DELETION`이 로드 시 주입됩니다.)

> **주의 — 아래 8·9단계의 `feast` CLI는 `.env`를 읽지 않습니다(#399).**
> Python 경로(`autoresearch.jobs.*`, 검증 스크립트)는 `feature_repo/bootstrap.py`가
> `FEAST_ONLINE_FULL_SCAN_FOR_DELETION`을 `AUTORESEARCH_ENV`에서 파생해
> 채워 주지만, `feast` CLI는 bootstrap을 거치지 않아 그 치환이 해소되지
> 않습니다. CLI를 쓰기 전에 `.env`를 셸로 내보내십시오.
>
> ```bash
> set -a; source .env; set +a
> ```

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
set -a; source .env; set +a    # feast CLI는 .env를 읽지 않습니다 (6단계 주의 참조)
cd feature_repo
feast apply
```

> Registry가 `GCS_REGISTRY_PATH`로 지정한 GCS 버킷에 저장됩니다.
>
> `${FEAST_ONLINE_FULL_SCAN_FOR_DELETION} is not a valid boolean` 류의 오류가
> 나면 위 `source .env`를 건너뛴 것입니다.

> **운영 경로 (GHA 셀프 호스티드 러너, 정본, #561)**: `feature_repo/`
> 정의·설정, `scripts/fetch_redis_ca.py`, `scripts/verify_registry_portability.py`,
> `pyproject.toml`, `uv.lock`, `.github/workflows/feast-apply.yml` 중 하나가
> `main`/`dev`에 merge되면 `.github/workflows/feast-apply.yml` 워크플로우가
> ARC 셀프 호스티드 러너(`feast-apply-prod`/`feast-apply-dev`) 위에서 feast
> 공식 CLI(`feast apply`)를 **직접 실행**해 GCS registry를 자동 갱신합니다.
> `workflow_dispatch`로 수동 실행도 가능합니다.
>
> 러너 자체가 VPC 안에서 돌기 때문에(인프라 저장소 #541/#557/#558) private
> Redis(PSC)에 직접 닿습니다. `full_scan_for_deletion: true`(아래 절)가 요구하는
> 스캔도 이 러너 위에서 그대로 수행됩니다. 2026-08 이전에는 GHA hosted 러너가
> VPC 밖이라 GKE Job으로 실행을 우회했으나(#346), 러너가 VPC 안으로 옮겨지며
> 그 우회가 불필요해졌습니다. 트리거 의미와 "registry apply는 GHA 소유,
> materialize는 Airflow 소유"라는 경계는 그대로입니다.
>
> feast 0.64의 apply 커맨드는 인증 실패(`FeastProviderLoginError`)를 삼키고
> exit 0으로 끝나는 결함이 있어, 워크플로우는 스텝 종료 코드 외에 두 가지
> 가드를 함께 둡니다 — apply 로그의 실패 패턴 grep, apply 전후 registry
> generation 비교.
>
> `actions/checkout`이 저장소를 러너에 직접 체크아웃하므로 GCS 코드 아카이브
> 부트스트랩(옛 `Dockerfile.feast` ENTRYPOINT)은 더 이상 필요 없습니다. GKE
> Job 경로(`deploy/feast/apply-job.yaml`, `Dockerfile.feast`)는 롤백 여유로
> 저장소에 남아 있지만 이 워크플로우는 더 이상 그 경로를 쓰지 않습니다.
>
> **DAG 소비용 경로 (폐기 완료)**: registry apply를 DAG에서 소비하기 위한 공개
> batch 명령 `python -m autoresearch.jobs.feast_apply` 래퍼가 존재했던
> 이유도 위와 같다 — feast 0.64 CLI가 `FeastProviderLoginError`를 exit 0으로
> 삼키는 결함을 우회해야 했다. 이 래퍼는 GHA `feast-apply` 워크플로우의 침묵
> 실패 가드(registry generation 비교 + apply 로그 grep)로 동일한 안전성을
> 확보하게 되면서 삭제되었다(#331). Airflow `feast_online_store_materialize`
> DAG의 `apply_feature_registry` 태스크도 함께 제거된다
> (`SKYAHO/Autoresearch-airflow#130`).

### `full_scan_for_deletion: true` (고아 키·필드 정리)

`feature_store.yaml`의 `online_store.full_scan_for_deletion: true`는
FeatureView 정의를 삭제하는 merge가 발생했을 때 apply가 Redis 키스페이스를
스캔해 해당 FeatureView의 흔적을 지우도록 합니다.

Feast 0.64의 Redis 키는 **엔티티 단위**(join_key 이름 + 엔티티 값 + project)
이고 FeatureView와 무관합니다. 같은 엔티티를 쓰는 FeatureView들이 하나의 키를
공유하고, FeatureView별 데이터는 HASH 필드로 구분됩니다. 그래서 Feast는 두
경우를 나눠 처리합니다.

- 삭제된 FV가 그 키의 유일한 FV → 키 자체를 `delete`
- 다른 살아있는 FV와 키를 공유 → 그 FV의 해시 필드만 `hdel`

이 스캔은 Redis 접속을 요구합니다. `feast-apply.yml`이 도는 셀프 호스티드
러너는 VPC 안에서 돌아 private Redis(PSC)에 직접 닿으므로 apply를 러너 위에서
바로 실행합니다(#561).

삭제 건수는 feast의 `logger.debug`로만 남기 때문에 apply 스텝은
`feast --log-level debug apply`로 실행합니다(기본값은 `warning`). 삭제 경로를
실증할 때는 로그에서 `Deleted N rows for feature view ...`를 확인합니다. 삭제
대상이 없는 apply는 Redis 클라이언트를 만들지도 않으므로, "apply 1회 성공"은
Redis 설정 검증이 되지 못합니다.

이 설정은 GHA apply 경로뿐 아니라 Airflow DAG가 같은 `feature_store.yaml`을
읽는 경로에도 적용되는 **공유 설정**입니다.

### GitHub repo variables ↔ Airflow 주입 env 값 일치

GHA `feast-apply` 경로와 Airflow DAG의 materialize 태스크는 **동일한
`feature_store.yaml`**을 서로 다른 실행 환경에서 채워 넣습니다. 두 값이
어긋나면 registry에 기록된 `BigQuerySource` 테이블 경로 등이 실행할 때마다
서로 다른 값으로 번갈아 덮어써지는 flip-flop이 발생합니다. 아래 변수는 반드시
두 경로에서 동일한 값이어야 합니다.

GHA는 값을 직접 소비하지 않고 **Job 매니페스트의 env로 주입**합니다.

| 변수 | GHA (repo variable → Job env) | Airflow (주입 env) |
|------|-------------------------------|---------------------|
| `GCP_PROJECT_ID` | `vars.GCP_PROJECT_ID` | 동일 값 |
| `BQ_DATASET` | `vars.BQ_DATASET` | 동일 값 |
| `GCS_REGISTRY_PATH` | `vars.GCS_REGISTRY_PATH` | 동일 값 |
| `GCS_STAGING_LOCATION` | `vars.GCS_STAGING_LOCATION` | 동일 값 |
| `REDIS_HOST` | `vars.REDIS_HOST` | 동일 값 |
| `REDIS_PORT` | `vars.REDIS_PORT` | 동일 값 |
| `REDIS_TLS_CA_PATH` | `/tmp/redis-ca.pem` (Job이 조달해 기록) | Airflow 쪽 값(설정 시) |

`feast` 공식 CLI에는 TLS CA를 조달하는 훅이 없습니다(래퍼 CLI가 담당하던
책임으로, #331에서 래퍼를 삭제하면서 조달 경로가 비었습니다). Memorystore는
인스턴스별 사설 CA를 쓰므로 시스템 신뢰 저장소로는 검증되지 않습니다. 그래서
Job은 `scripts/fetch_redis_ca.py`로 Secret Manager의 CA를 고정 경로
`/tmp/redis-ca.pem`에 먼저 내려받은 뒤 `feast apply`를 실행하고,
`REDIS_TLS_CA_PATH`를 그 경로로 정적 주입합니다.

#### apply Job 전용 repo variables

| 이름 | 예시 | 용도 |
|------|------|------|
| `GKE_CLUSTER_NAME` | `autoresearch-dev-gke` | Job을 만들 클러스터 |
| `GKE_LOCATION` | `asia-northeast3-a` | 클러스터 위치(zonal) |
| `FEAST_IMAGE_TAG` | `sha-<40자 커밋 SHA>` | feast 이미지 고정 태그 |
| `REDIS_CA_SECRET_ID` | `autoresearch-dev-redis-server-ca` | Redis 서버 CA secret |
| `FEAST_APPLY_NAMESPACE` | `feast-apply` (기본값) | infra#346 전용 namespace |
| `FEAST_APPLY_SERVICE_ACCOUNT` | `feast-apply` (기본값) | infra#346 KSA |

이미지 태그는 고정하고 실행할 코드 버전은 `CODE_ARCHIVE_SHA`(트리거 커밋)로
핀 고정합니다. `Dockerfile.feast`는 코드를 담지 않으므로 "정의가 이미지에
포함된" 경우는 존재하지 않습니다.

- [ ] 완료

---

## 9. Materialize (BigQuery → Redis 동기화)

```bash
set -a; source .env; set +a    # feast CLI는 .env를 읽지 않습니다 (6단계 주의 참조)
# feature_repo/ 디렉토리 안에서 실행
feast materialize-incremental $(date -u +"%Y-%m-%dT%H:%M:%S")
```

> BigQuery 데이터를 읽어 GCS Staging을 거쳐 Redis에 적재합니다.

- [ ] 완료

### Redis 키 TTL (`key_ttl_seconds`)

`feature_store.yaml`의 `online_store.key_ttl_seconds: 604800`(7일)은 Redis에
키를 쓸 때마다 EXPIRE를 거는 Feast 0.64 online store 설정입니다. 고아 정리의
주 수단은 `full_scan_for_deletion`이고, TTL은 **심층 방어**입니다.

- 살아있는 키는 매일 실행되는 materialize가 매번 다시 써서 TTL이 매일
  리셋되므로 계속 서빙됩니다.
- 아무도 쓰지 않게 된 키(예: 엔티티 자체가 폐기되어 그 키를 쓰는
  FeatureView가 하나도 남지 않은 경우)는 TTL이 리셋되지 않고 7일 후 Redis가
  자동 소멸시킵니다.
- **삭제된 FeatureView의 고아를 TTL로 지울 수는 없습니다.** EXPIRE는 키
  단위이지 해시 필드 단위가 아니기 때문입니다. 삭제된 FV가 살아있는 FV와
  엔티티를 공유하면(이 저장소의 `UserStaticView`·`UserDynamicView`가
  `user_entity`를 공유하는 경우가 그렇습니다) 살아있는 FV의 매일 쓰기가 같은
  키의 EXPIRE를 리셋하므로 고아 필드는 영구히 남습니다. 이 경로는
  `full_scan_for_deletion: true`의 `hdel`이 담당합니다.
- 트레이드오프: materialize가 7일 이상 연속 실패하면 살아있는 서빙 키도
  함께 만료되어 조회가 빈 값을 반환합니다. 즉 materialize 장애는 7일 이내
  복구를 전제로 합니다.
- FeatureView 정의의 `ttl` 파라미터와는 다른 개념입니다. `ttl`은 조회
  시점에 값이 "신선한지"를 판정하는 기준이고, `key_ttl_seconds`는 Redis
  키 자체의 물리적 만료 시간입니다.

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

## 부록: 무인 실행용 Vertex AI 서비스 계정 (재인증 없이)

`gcloud auth application-default login`으로 얻은 개인 계정 ADC 세션은 일정
기간이 지나면 만료되고(capability probe round_002에서 라운드 사이에 만료 실측,
#426), 만료되면 사람이 브라우저로 다시 인증해야 합니다. 에이전트가 정책
시뮬레이션 라운드를 사람 개입 없이 이어서 돌리려면 서비스 계정 키를 쓰는 편이
낫습니다. **코드 변경은 필요 없습니다** — `google.auth.default()`가
`GOOGLE_APPLICATION_CREDENTIALS`를 gcloud ADC보다 먼저 확인하므로, 환경 변수만
지정하면 Vertex AI 임베딩 호출이 그대로 서비스 계정으로 나갑니다.

1. "1. 서비스 계정 생성"의 절차로 서비스 계정(`feast-sa`)을 만들거나 기존 것을
   재사용하고, 키를 `keys/service-account.json`으로 내려받습니다.
2. Vertex AI 임베딩 호출 권한을 추가로 부여합니다:

   ```bash
   SA_EMAIL="feast-sa@${GCP_PROJECT}.iam.gserviceaccount.com"

   gcloud projects add-iam-policy-binding $GCP_PROJECT \
     --member="serviceAccount:${SA_EMAIL}" --role="roles/aiplatform.user"
   ```

3. `.env`(또는 셸)에서 키 파일 경로를 지정합니다:

   ```bash
   export GOOGLE_APPLICATION_CREDENTIALS="./keys/service-account.json"
   ```

4. 이후 `gcloud auth application-default login` 재인증 없이 계속 동작합니다.
   **키 파일은 시크릿이므로 커밋 금지**입니다.

정책 시뮬레이션 라운드는 시작 시점에 자격증명을 한 번 점검하므로(duckdb 조립
경로 한정, `autoresearch/feature_engineering/embeddings.py`의 `verify_vertex_ai_credentials`),
세션이 만료된 상태면 5단계를 다 돌린 뒤가 아니라 즉시 실패합니다.

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
| `GCP 자격증명 세션이 만료됐습니다` (정책 라운드 시작 직후) | ADC 세션 만료 — `gcloud auth application-default login` 재인증, 또는 위 "부록: 무인 실행용 Vertex AI 서비스 계정"으로 전환 |
