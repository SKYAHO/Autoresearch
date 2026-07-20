# Dockerfile.feast 부트스트랩 전환 — 파드 시작 시 GCS 코드 주입

- **상태**: Approved
- **날짜**: 2026-07-18
- **이슈**: #181
- **관련 문서**:
  - `docs/specs/2026-07-18-code-archive-gcs-upload.md` (#174 — 소비자 계약 §6)
  - `docs/specs/2026-07-15-feast-redis-online-store.md` (#148)
  - `docs/runbooks/2026-07-15-feast-redis-gke-validation.md`

## 목적

`Dockerfile.feast`는 `autoresearch/`, `feature_repo/`를 이미지에 COPY하므로
코드가 바뀔 때마다 이미지를 재빌드해야 한다. 이미지를 실행 환경(Python +
feast 그룹 의존성)만 담도록 슬림화하고, 파드 시작 시 부트스트랩 스크립트가
GCS 코드 아카이브(#174 파이프라인 산출물)를 내려받아 `/app`에 풀고 전달받은
커맨드를 실행하게 전환한다. 이후 이미지 재빌드는 의존성 변경 시에만 필요하다.

## 현재 상태

- `Dockerfile.feast`(main, #154 머지): uv lock 기반 feast 그룹 설치 후
  `COPY autoresearch ./autoresearch`, `COPY feature_repo ./feature_repo`,
  CMD는 `import feast, feature_repo.redis_iam` 스모크.
- `ci.yml`의 feast 이미지 검증: 빌드 후 bare `docker run`(CMD 스모크)과
  `feast_materialize --help/--version` 실행 — 코드가 이미지에 있다는 전제.
- #174가 main 머지 시 `gs://<bucket>/code/<sha>.tar.gz` + `code/latest.txt`를
  적재한다 (PR #180, 병행 진행 중 — 파일 미중복).
- feast 의존성 그룹에 `google-cloud-storage`가 이미 포함되어 있다
  (`feast[gcp]` 전이 의존성, uv.lock 고정).

## 결정

### 1. 부트스트랩 스크립트 — `scripts/feast_bootstrap.sh`

이미지 ENTRYPOINT. #174 spec §6 소비자 계약을 구현한다.

**입력 (env):**

| 변수 | 필수 | 의미 |
| --- | --- | --- |
| `CODE_ARTIFACTS_BUCKET` | GCS 모드에서 필수 | 코드 아카이브 버킷 이름 (`gs://` 제외) |
| `CODE_ARCHIVE_SHA` | 선택 | 지정 시 해당 40자 SHA 고정 실행. 미지정 시 `code/latest.txt`에서 읽음 (개행 trim) |
| `CODE_ARCHIVE_LOCAL_PATH` | 선택 | 지정 시 GCS를 건너뛰고 이 경로의 tar.gz 사용 (CI·로컬 검증용). 이때 `CODE_ARTIFACTS_BUCKET` 불필요 |

**동작 순서:**

1. `CODE_ARCHIVE_LOCAL_PATH`가 있으면 그 파일을 아카이브로 사용
2. 없으면 SHA 확정(`CODE_ARCHIVE_SHA` 또는 latest.txt) 후
   `code/<sha>.tar.gz` 다운로드 — GCS 접근은 python 인라인
   (`google.cloud.storage`, ADC 자격 증명 자동 사용: GKE에서는 Workload
   Identity, 로컬에서는 gcloud ADC)
3. `/app`(WORKDIR)에 `tar -xzf` — 아카이브에 최상위 래핑 디렉토리가 없어
   저장소 루트 구조가 그대로 풀린다
4. 실행 코드 버전을 stdout에 로그 (`[feast-bootstrap] code: <sha|local>`)
5. `exec "$@"` — Airflow KPO가 전달한 커맨드(또는 이미지 기본 CMD) 실행

**에러 처리:** `set -euo pipefail`. `CODE_ARCHIVE_LOCAL_PATH`도
`CODE_ARTIFACTS_BUCKET`도 없으면 명확한 메시지로 즉시 실패. 아카이브 부재
시 대상 `gs://` 경로를 포함해 실패. 권한 부족(403 Forbidden)은 파드 GSA에
버킷 `roles/storage.objectViewer`가 필요함을, 자격 증명 부재
(DefaultCredentialsError)는 Workload Identity/ADC 설정 확인을 안내하는
메시지로 exit 2 처리한다 (traceback 노출 방지). 인자 없이 실행되면(`"$@"` 비어 있음)
사용법을 출력하고 실패 — 단, 이미지 CMD가 기본 인자를 제공하므로 일반
경로에서는 발생하지 않는다.

### 2. `Dockerfile.feast` 변경

- `COPY autoresearch`, `COPY feature_repo` 제거
- `COPY scripts/feast_bootstrap.sh /usr/local/bin/feast_bootstrap.sh` —
  `/app`은 압축 해제 대상이라 밖에 둬야 코드에 덮이지 않는다
- `RUN chown -R appuser /app` — appuser(비루트)가 압축 해제 가능하도록.
  `-R`이 필요한 이유: `/app`에는 root 소유로 COPY된 `pyproject.toml`,
  `uv.lock`이 있고, 아카이브에도 같은 파일이 있어 tar가 덮어쓰기 때문
- `ENTRYPOINT ["/usr/local/bin/feast_bootstrap.sh"]`
- `CMD ["python", "-c", "import feast, feature_repo.redis_iam; print('autoresearch feast image ready')"]`
  유지 — 이제 부트스트랩이 코드를 푼 뒤에 실행되므로 여전히 유효한 스모크
- `VCS_REF`/`AUTORESEARCH_REVISION`/revision 라벨은 "이미지 빌드 시점 커밋"
  의미로 유지. 실행 코드 버전은 부트스트랩 로그가 담당 (이미지 버전과 코드
  버전이 분리되는 것이 이 전환의 목적)

### 3. `ci.yml` feast 이미지 검증 변경

`Run Feast Docker image smoke check` 스텝을 로컬 아카이브 주입 모드로
전환한다:

1. `git archive --format=tar.gz -o /tmp/code-archive.tar.gz HEAD`
2. 각 `docker run`에 `-v /tmp/code-archive.tar.gz:/tmp/code-archive.tar.gz:ro
   -e CODE_ARCHIVE_LOCAL_PATH=/tmp/code-archive.tar.gz` 추가
3. 기존 세 검증(기본 CMD 스모크, `feast_materialize --help`, `--version`)
   유지

GCS 없이 부트스트랩의 압축 해제 → 커맨드 실행 경로 전체가 CI에서 검증된다.

### 4. runbook 갱신

`docs/runbooks/2026-07-15-feast-redis-gke-validation.md`의 pod 실행 절차에
부트스트랩 env를 반영한다: pod env에 `CODE_ARTIFACTS_BUCKET`(및 선택적
`CODE_ARCHIVE_SHA`) 추가, "이미지를 코드 변경마다 재빌드" 전제 제거. GCS
모드 E2E 검증 절차(파드 로그에서 code SHA 확인)를 추가한다.

## 경계·의존성

- **선행 완료:** #154 (Dockerfile.feast main 머지 — 완료)
- **병행:** PR #180 — 수정 파일 미중복이라 구현은 병행 가능하나, GCS 모드
  E2E는 #180 머지 + 인프라(버킷·업로더 SA·secret) 이후 가능
- **인프라 (Autoresearch-infra):** 파드 GSA에 버킷
  `roles/storage.objectViewer` (#174 spec §5에 명시)
- **Airflow (Autoresearch-airflow):** KPO env 전달
  (`CODE_ARTIFACTS_BUCKET`, `CODE_ARCHIVE_SHA`)은 인접 저장소 소관

## 검증

- CI 로컬 아카이브 모드가 자동 회귀 검증 (압축 해제·exec 경로)
- 로컬: `git archive`로 tar.gz 생성 → `docker run -v ... -e
  CODE_ARCHIVE_LOCAL_PATH=...`로 세 검증 재현
- 에러 경로: env 없이 `docker run` → 명확한 실패 메시지 확인
- E2E (후속): GKE 파드에서 GCS 모드 실행, 파드 로그의 code SHA 확인
  (runbook 절차)

## 범위 밖

- `Dockerfile.app`의 동일 방식 전환 여부 (별도 논의)
- Airflow DAG env 배선 (`Autoresearch-airflow`)
- 버킷·IAM 프로비저닝 (`Autoresearch-infra`)
