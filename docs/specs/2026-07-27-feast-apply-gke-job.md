# feast apply GKE Job 전환 — 설계 결정

- 이슈: SKYAHO/Autoresearch#346 (선행: SKYAHO/Autoresearch-infra#346)
- 상태: 구현 완료

## 배경

`key_ttl_seconds: 604800`(#323/#324)은 삭제된 FeatureView의 고아를 절반만
지웁니다. Feast 0.64의 Redis 키는 엔티티 단위라 FeatureView들이 키를 공유하고,
`key_ttl_seconds`는 쓰기마다 **키 전체**에 EXPIRE를 겁니다. 삭제된 FV가
살아있는 FV와 엔티티를 공유하면 살아있는 FV의 매일 materialize가 EXPIRE를
리셋하므로 고아 해시 필드가 영구히 남습니다. 이 저장소의 `UserStaticView`와
`UserDynamicView`가 모두 `user_entity`를 쓰므로 그 조건에 해당합니다.

`full_scan_for_deletion: true`면 Feast가 두 경우를 구분합니다 — 그 키를 쓰는
FV가 하나뿐이면 `delete`, 공유 중이면 `hdel`. 이 스캔은 Redis 접속을
요구하는데 GHA 러너는 VPC 밖이라 닿지 못합니다.

## 결정

**apply의 실행 위치만 VPC 안의 GKE Job으로 옮깁니다.** GHA는 Job을 만들고
결과를 판정합니다. 트리거 주체(GHA)와 소유 경계("registry apply는 GHA,
materialize는 Airflow", #331)는 바뀌지 않으며 래퍼 CLI도 부활시키지 않습니다.

### S1. `command`가 아니라 `args`

`Dockerfile.feast`는 코드를 이미지에 넣지 않습니다. ENTRYPOINT
(`scripts/gcs_code_bootstrap.sh`)가 파드 시작 시 GCS 코드 아카이브를 `/app`에
풀고 전달받은 커맨드를 `exec`합니다. `command:`를 지정하면 부트스트랩이
대체되어 `feature_repo/`가 없는 컨테이너에서 apply가 돌게 됩니다. 따라서
Job은 **`args`만** 지정합니다(`deploy/feast/apply-job.yaml`).

`--log-level debug`가 필수입니다. feast CLI 기본값이 `warning`이라 삭제 건수
로그(`redis.py`의 `logger.debug`)가 남지 않아 삭제 경로를 실증할 수 없습니다.

### S2. 이미지 태그가 아니라 코드 아카이브 SHA

이미지에는 정의가 들어있지 않으므로 "어느 이미지 태그를 쓸 것인가"는 정의
버전과 무관합니다. 결정 대상은 **어느 커밋의 정의를 apply할 것인가**입니다.

- 이미지 태그는 repo variable `FEAST_IMAGE_TAG`로 고정
- 코드 버전은 `CODE_ARCHIVE_SHA=${{ github.sha }}`로 핀 고정
  (미지정 시 부트스트랩이 `code/latest.txt` = 직전 커밋을 참조)
- `CODE_ARTIFACTS_BUCKET`은 없으면 부트스트랩이 exit 2

### S3. 코드 아카이브 업로드와의 경쟁 조건

`code-archive.yml`과 `feast-apply.yml`은 둘 다 main push에 트리거되고
concurrency 그룹이 분리돼 병렬로 돕니다.

`workflow_run` 전환은 code-archive가 모든 main push에 돌기 때문에
feast-apply의 path 필터를 잃습니다. 그래서 **아카이브 객체 존재 폴링**을
택했습니다 — `gs://<bucket>/code/<sha>.tar.gz`를 10초 간격으로 최대 10분
확인하고, 없으면 원인을 명시하며 실패합니다.

폴링을 택하면 `code/latest.txt`(직전 커밋 정의로 apply하고도 registry
generation이 바뀌어 침묵 실패 가드를 통과하는 경로)를 쓸 일이 없어집니다.

### S4. Redis TLS CA 조달 — 고정 경로

`feature_store.yaml`의 `tls_ca_cert_path: ${REDIS_TLS_CA_PATH}`를 채우는
`ensure_redis_ca_bundle()`은 `feast apply` 경로에서 호출되지 않습니다(호출처는
serving reader와 materialize job뿐). CA가 없으면 `redis_iam.py`가 시스템 CA로
검증하는데, Memorystore는 인스턴스별 사설 CA라 검증이 실패합니다.
`full_scan_for_deletion: false` 동안에는 apply가 Redis에 접속하지 않아 이
문제가 드러나지 않았습니다.

**(i) 고정 경로 조달**을 택했습니다. Secret Manager CSI 드라이버(ii)는 클러스터
애드온 활성화가 선행돼야 하고 인프라 의존이 하나 더 늘어납니다.

- `feature_repo/bootstrap.py`에 `download_redis_ca_bundle(destination)` 추가.
  기존 `ensure_redis_ca_bundle`은 임시 경로를 호출 프로세스 env에만 심으므로
  CA 조달과 feast 실행이 다른 프로세스인 이 경로에는 쓸 수 없습니다.
- `scripts/fetch_redis_ca.py`가 그 함수의 CLI 표면. Job args가
  `fetch_redis_ca.py /tmp/redis-ca.pem && feast apply` 순서로 실행합니다.
- `REDIS_TLS_CA_PATH=/tmp/redis-ca.pem`을 Job env로 정적 주입합니다.

feast 래퍼 CLI를 되살리는 것이 아니라, 래퍼가 담당하던 CA 조달 책임만
분리된 스크립트로 옮긴 것입니다.

## 실패 판정

Job 종료 코드에 더해 세 가드를 둡니다(`.github/workflows/feast-apply.yml`).

| 가드 | 잡는 실패 |
|---|---|
| Job Complete/Failed 조건 판정 | 부트스트랩 exit 2, Workload Identity 실패, apply 예외 |
| `[gcs-bootstrap] code: <sha>` 로그 단언 | 성공했지만 다른 커밋의 정의를 apply한 경우 |
| `application-default login\|GCP error:` grep | feast 0.64가 `FeastProviderLoginError`를 삼키고 exit 0 |
| registry generation 비교 | 위와 동일 계열의 침묵 실패 |

`kubectl wait --for=condition=complete`는 실패 시 조건이 서지 않아 타임아웃까지
대기하고 원인을 "timeout"으로 오인시킵니다. 그래서 Complete/Failed 두 조건을
함께 폴링하고, 로그 수집은 `if: always()` + 실패 허용으로, 실패 시
`kubectl describe`를 추가로 출력합니다.

기존 grep 패턴은 GHA 러너의 ADC 실패 모드를 겨냥한 것이었습니다. Pod 실행
모드에서 부트스트랩·WI 실패는 exit 2(한국어 메시지)로 드러나 Job 결과 가드가
잡으므로 패턴을 늘리는 대신 코드 SHA 단언을 추가했습니다.

## 운영 비용

`delete_table`은 prefix 패턴으로 전체 키스페이스를 SCAN한 뒤 모든 키를 파이썬
리스트에 적재합니다. 사용자 키가 늘면 Job 메모리와 Redis 부하가 키스페이스
규모에 비례합니다. 초기값은 requests 500m/2Gi, limits 2/4Gi이며 키스페이스
증가에 맞춰 조정합니다.

## 검증

- `uv run python -m pytest`, CI `pytest (feast group)` 목록
- 삭제 경로 실증: 임시 FV 추가 → apply → 삭제 → apply 후 Job 로그에서
  `Deleted N rows for feature view ...` 확인. 삭제 대상이 없는 apply는 Redis
  클라이언트를 만들지도 않으므로 "apply 1회 성공"은 검증이 되지 못합니다.
