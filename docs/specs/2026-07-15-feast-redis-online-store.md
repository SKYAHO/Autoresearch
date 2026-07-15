# Feast Online Store Memorystore Redis Cluster 연동

- **상태**: Proposed
- **날짜**: 2026-07-15
- **이슈**: #148
- **관련 문서**:
  - `Autoresearch-infra/docs/superpowers/specs/2026-07-11-online-store-redis-design.md` (infra #129)
  - `docs/specs/2026-07-13-public-batch-execution-contract.md`

## 목적

`Autoresearch-infra` #129로 배포된 Memorystore for Redis Cluster를 Feast
Online Store로 연동한다. BigQuery offline store의 feature를 Redis Cluster에
materialize하는 공개 batch CLI와 실행 이미지를 제공하고, GKE pod에서 실제
연결·적재·온라인 조회를 검증한다.

인프라 설계 문서는 "Feast 0.64.0의 실제 호환성 및 adapter 구현"을 이 저장소
후속 작업의 merge 전제 조건으로 명시했다. 이 spec이 그 후속 작업이다.

## 현재 상태

- `feature_repo/feature_store.yaml`의 online store는
  `connection_string: ${REDIS_HOST}:${REDIS_PORT}` 평문 단일 Redis 구성이다.
- 배포된 Redis Cluster는 다음을 요구하며 현재 구성으로는 연결할 수 없다.
  - cluster 모드 (2-shard, discovery endpoint `:6379`, data node
    `:11000-13047`)
  - TLS 서버 인증 (Google-managed per-instance CA, CA 번들은 Secret Manager
    저장)
  - IAM 인증 (`AUTH_MODE_IAM_AUTH`) — 정적 비밀번호 없음, Workload Identity로
    발급한 단기 IAM access token을 `AUTH`에 사용
- PSC 전용 접근이므로 dev VPC 내부(GKE `autoresearch` namespace pod)에서만
  접근할 수 있다. 로컬 개발 환경에서는 실제 cluster에 연결할 수 없다.
- feast 의존성 그룹(`feast[gcp]==0.64.0`, `redis>=5.0`)은 dev/proxy 그룹과
  충돌로 격리되어 있고 `Dockerfile.app`은 feast를 포함하지 않는다.
- app GSA에는 대상 cluster 한정 `roles/redis.dbConnectionUser`와 CA secret
  `roles/secretmanager.secretAccessor`가 부여되어 있다 (infra 소유).

## 결정

### 1. IAM 인증 어댑터 — `feature_repo/redis_iam.py`

Feast 0.64 `RedisOnlineStore`는 `_get_client`/`_get_client_async` 한 쌍에서
`connection_string` 파싱 결과로 client를 생성한다. 정적 password만 지원하므로
만료되는 IAM token을 다룰 수 없다. 다음 두 구성 요소를 추가한다.

**`GCPIAMCredentialProvider`** — redis-py `CredentialProvider` 구현.

- `google.auth.default()` 자격 증명에서 access token을 발급한다.
- token은 프로세스 내 캐시하고 만료 5분 전부터 선제 갱신한다.
- redis-py는 신규 연결마다 `get_credentials()`를 호출하므로 재연결,
  topology refresh, `MOVED` redirect 시에도 항상 유효한 token으로 `AUTH`한다.
- Memorystore IAM 인증의 `AUTH` username 규약(단일 token 인자 vs
  `default`+token)은 구현 시 공식 문서와 실기기 검증으로 확정한다.

**`IAMRedisOnlineStore(RedisOnlineStore)`** + **`IAMRedisOnlineStoreConfig`**

- `_get_client`를 오버라이드해 부모의 connection string 파싱은 재사용하되
  client kwargs에 다음을 주입한다.
  - `credential_provider=GCPIAMCredentialProvider(...)`
  - `ssl=True`, `ssl_ca_certs=<CA 번들 경로>`
- `_get_client_async`는 feature server 전용 경로로 이 연동 범위 밖이므로,
  미검증 인증 경로 사용을 막기 위해 `NotImplementedError`로 명시적으로
  차단한다.
- Feast 커스텀 online store 규약(`get_online_config_from_type`)을 따른다:
  클래스 이름은 `OnlineStore`로 끝나야 하고, 같은 모듈에 `<클래스명>Config`
  pydantic 설정 클래스가 있어야 한다.
- `IAMRedisOnlineStoreConfig`는 `RedisOnlineStoreConfig`를 상속하고 `type`을
  모듈 경로 literal로 재정의하며, 다음 필드를 추가한다.
  - `iam_auth: bool` (기본 `true`) — `false`면 부모 동작 그대로 (로컬
    fakeredis/단일 Redis 테스트용)
  - `tls_ca_cert_path: str | None` — CA 번들 파일 경로
- `feature_repo` 모듈은 materialize CLI가 `sys.path`에 repo root를 보장한 뒤
  import한다. `feast apply`를 `feature_repo/`에서 직접 실행하는 경우를 위해
  모듈 경로 해석 방식을 검증 절차에 포함한다.

### 2. `feature_store.yaml` 갱신

```yaml
online_store:
  type: feature_repo.redis_iam.IAMRedisOnlineStore
  redis_type: redis_cluster
  connection_string: ${REDIS_HOST}:${REDIS_PORT}
  tls_ca_cert_path: ${REDIS_TLS_CA_PATH}
```

- `REDIS_HOST`/`REDIS_PORT`는 discovery endpoint 값을 재사용한다 (기존 env
  이름 유지).
- `iam_auth`는 yaml에 두지 않는다. 기본값 `true`를 사용하며, `iam_auth=false`
  폴백(로컬 단일 Redis/fakeredis)은 테스트에서 config를 직접 생성할 때만
  쓴다. 미설정 `${...}` env가 literal로 남으면 bool 파싱이 깨지기 때문이다.
- Feast registry, offline store 설정은 변경하지 않는다.

### 3. CA 번들 조달

- `REDIS_TLS_CA_PATH`에 파일이 있으면 그대로 사용한다.
- 없으면 `REDIS_CA_SECRET_ID`(Secret Manager secret id)에서 번들을 읽어
  프로세스 임시 파일로 저장하고 그 경로를 사용한다.
- Secret Manager 접근은 materialize CLI 시작 시 1회 수행한다. CA 번들은
  비밀은 아니지만 로그에 본문을 출력하지 않는다.
- `google-cloud-secret-manager`를 feast 의존성 그룹에 추가한다.

### 4. materialize 공개 CLI — `autoresearch/jobs/feast_materialize.py`

공개 배치 계약 v1에 호환 추가 명령으로 등록한다 (계약 spec 갱신 포함).

```text
python -m autoresearch.jobs.feast_materialize \
  [--views VIEW1,VIEW2] \
  [--start-ts ISO8601 --end-ts ISO8601] \
  [--dry-run[=<boolean>]]
```

- `--start-ts`/`--end-ts` 둘 다 없으면 `materialize-incremental`(끝 시각은
  현재 UTC), 둘 다 있으면 구간 `materialize`를 수행한다. 하나만 주면 exit 2.
- `--views` 생략 시 registry의 전체 FeatureView를 대상으로 한다.
- `--dry-run`은 CA 조달, IAM token 발급, Redis `PING`, registry 접근까지만
  검증하고 적재 없이 exit 0으로 종료한다.
- `--help`/`--version`, JSON Lines stdout, `job_summary` 마지막 event,
  exit code 0/1/2 의미는 기존 계약 공통 규칙을 따른다.
- secret은 CLI 인자로 받지 않는다. 필요한 환경 변수는 아래 표와 같다.

| 이름 | 용도 | 비고 |
| --- | --- | --- |
| `GCP_PROJECT_ID`, `BQ_DATASET`, `GCS_REGISTRY_PATH`, `GCS_STAGING_LOCATION` | 기존 Feast 설정 | 변경 없음 |
| `REDIS_HOST`, `REDIS_PORT` | discovery endpoint | 기존 이름 재사용 |
| `REDIS_TLS_CA_PATH` | CA 번들 파일 경로 | 선택 |
| `REDIS_CA_SECRET_ID` | CA 번들 Secret Manager id | `REDIS_TLS_CA_PATH` 부재 시 필수 |

`.env.example`을 같은 표 기준으로 갱신한다.

### 5. 실행 이미지 — `Dockerfile.feast`

- `Dockerfile.app`과 같은 uv lock-export 패턴을 사용하되 feast 그룹을
  포함해 export한다 (`uv export --frozen --no-dev --group feast`).
- `autoresearch/`와 `feature_repo/`를 함께 복사한다.
- OCI label은 app 이미지와 같은 규칙을 사용한다.
- Airflow KPO의 materialize task와 GKE 검증 pod가 이 이미지를 사용한다.
  schedule·KPO 인자는 `Autoresearch-airflow` 소유로 이 spec 범위가 아니다.

### 6. 키 인코딩과 hash tag

Feast의 Redis key 인코딩(`_redis_key`)을 그대로 사용하고 hash tag를 도입하지
않는다. entity key별로 slot이 분산되며, redis-py `RedisCluster`가 명령을
slot별로 분할 실행하므로 `CROSSSLOT` 문제가 발생하지 않는다. 인프라 설계의
hash slot 학습 검증(`CLUSTER KEYSLOT`, `MGET` single-slot 제약 재현)은 GKE
검증 절차에 포함하되 Feast key 스키마 변경은 하지 않는다.

## 에러 처리

다음 실패를 구분 가능한 stderr 메시지와 exit code로 보고한다.

| 실패 | exit | 비고 |
| --- | --- | --- |
| CLI 인자 문법·조합 오류 | 2 | `--start-ts`만 지정 등 |
| IAM token 발급 실패 (ADC 부재 등) | 1 | google-auth 예외 원문 stderr |
| `AUTH` 거부 (`redis.dbConnectionUser` 미부여) | 1 | 권한 문제임을 메시지에 명시 |
| TLS 검증 실패 (CA 불일치) | 1 | CA 조달 경로 재확인 안내 |
| Secret Manager 접근 실패 | 1 | secret id와 권한 안내 |
| materialize 중 부분 실패 | 1 | Feast materialize는 upsert라 재실행 안전 |

token, CA 본문, entity 값은 로그에 출력하지 않는다 (기존 계약 로그 규칙).

## 테스트

feast·redis 의존성은 격리 그룹에만 있어 dev 환경(CI 기본 matrix 포함)에서는
import할 수 없다. 다음 방식으로 실행 환경을 분리한다.

- feast 그룹에 `pytest`를 추가하고, feast 어댑터 테스트
  (`tests/test_redis_iam.py`)는
  `uv run --no-dev --group feast python -m pytest tests/test_redis_iam.py`
  로 실행한다. CLI 테스트(`tests/test_feast_materialize.py`)는 feast import를
  지연시켜 dev 환경에서도 실행된다.
- 기존 CI matrix에서는 `tests/test_redis_iam.py`가 module 수준
  `pytest.importorskip("feast")`로 skip되어 수집 실패가 없다.
- CI에 feast 테스트 전용 job 추가 여부는 plan에서 결정한다 (최소한 로컬
  실행 절차는 문서화한다).

단위 테스트 (`tests/test_feast_materialize.py`, `tests/test_redis_iam.py`):

- `GCPIAMCredentialProvider`: google-auth mock으로 token 캐시, 만료 임박
  갱신, 발급 실패 전파를 검증한다.
- `IAMRedisOnlineStore._get_client`: `RedisCluster` 생성자를 mock해
  credential provider·ssl kwargs 주입과 `iam_auth=false` 폴백을 검증한다.
- CLI: 인자 파싱, exit code 0/1/2, `--dry-run` 분기, CA fetch 분기
  (Secret Manager client mock), `job_summary` event 형식을 검증한다.

## GKE 검증 절차

이번 작업에서 실제로 수행한다. 사전 조건: kubectl 접근 권한, 이미지 push
권한, BigQuery feature 테이블 더미 데이터
(`scripts/generate_and_upload_dummy_data.py`).

1. `Dockerfile.feast` 빌드 후 레지스트리에 push한다.
2. BigQuery에 더미 feature 데이터를 적재한다 (기존 스크립트 재사용).
3. `autoresearch` namespace에 KSA `autoresearch-app`으로 검증 pod를 띄운다.
4. pod 안에서 순서대로 검증한다.
   - `--dry-run`: CA 조달 → IAM token `AUTH` → TLS `PING` 성공
   - `CLUSTER SHARDS`로 2-shard topology 확인
   - `CLUSTER KEYSLOT`로 같은 hash tag key의 slot 일치, 다른 slot key의
     `MGET` `CROSSSLOT` 재현 (인프라 학습 목표 검증)
   - `feast apply` (GCS registry 갱신)
   - 실제 materialize 실행, exit 0과 `job_summary` 확인
   - `get_online_features`로 Redis에서 feature 값 조회 확인
     (`scripts/verify_feature_retrieval.py` 재사용 또는 확장)
5. 검증 pod를 삭제하고 결과를 이슈에 기록한다.

## 보안·비용·롤백

- IAM token은 메모리에서만 사용하고 저장·로그 출력하지 않는다.
- 정적 Redis 비밀번호는 어디에도 존재하지 않는다.
- 어댑터·CLI·Dockerfile은 신규 파일이므로 기존 동작에 영향이 없다.
  `feature_store.yaml`과 `.env.example` 변경만 되돌리면 이전 상태로 복원된다.
- Online store 데이터는 offline store에서 재-materialize 가능하므로 데이터
  롤백 부담이 없다 (인프라 설계 전제와 일치).
- nano 2-shard의 writable keyspace는 약 2.24 GB다. 더미 데이터 규모에서는
  문제없고, 실데이터 전환 시 `key_ttl_seconds`와 용량 산정을 별도 이슈로
  검토한다.

## 비목표

- 온라인 조회 serving 유틸리티·FastAPI serving (후속 이슈)
- hash tag 기반 커스텀 key 스키마 (Feast 기본 인코딩 유지)
- Airflow DAG·schedule·KPO 인자 (`Autoresearch-airflow` 소유)
- Feast registry·offline store 구성 변경
- 실데이터 feature 스키마 교체 (더미 스키마 유지)

## 참고 자료

- [Feast 0.64 RedisOnlineStore 소스](https://github.com/feast-dev/feast/blob/v0.64.0/sdk/python/feast/infra/online_stores/redis.py)
- [Feast custom online store](https://docs.feast.dev/how-to-guides/customizing-feast/adding-support-for-a-new-online-store)
- [redis-py CredentialProvider](https://redis.readthedocs.io/en/stable/examples/connection_examples.html)
- [Memorystore Redis Cluster IAM authentication](https://docs.cloud.google.com/memorystore/docs/cluster/about-iam-auth)
- [Memorystore Redis Cluster in-transit encryption](https://docs.cloud.google.com/memorystore/docs/cluster/about-in-transit-encryption)
