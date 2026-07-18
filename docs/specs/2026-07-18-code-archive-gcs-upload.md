# 코드 아카이브 GCS 업로드 파이프라인

- **상태**: Approved
- **날짜**: 2026-07-18
- **이슈**: #174
- **관련 문서**:
  - `docs/specs/2026-07-15-feast-redis-online-store.md` (#148)
  - `docs/runbooks/2026-07-15-feast-redis-gke-validation.md`
  - `docs/specs/2026-07-13-public-batch-execution-contract.md`

## 목적

Feast 실행 이미지(`Dockerfile.feast`)는 `autoresearch/`, `feature_repo/`를
이미지에 COPY하므로 코드가 바뀔 때마다 이미지를 재빌드해야 한다. 코드 배포를
이미지 빌드에서 분리한다:

- main 머지 시 저장소 추적 파일 전체를 압축해 GCS에 업로드하고 latest
  포인터를 갱신한다.
- 파드가 뜰 때 부트스트랩 스크립트가 GCS에서 아카이브를 내려받아 압축을
  풀고 전달받은 커맨드를 실행한다 (후속 이슈).
- 이미지 재빌드는 의존성(라이브러리) 변경 시에만 필요해진다.

이 spec은 첫 단계인 **업로드 파이프라인**만 다룬다. 이미지 부트스트랩 전환은
후속 이슈에서 이 spec의 소비자 계약을 참조해 진행한다.

## 결정

### 1. GCS 레이아웃과 버저닝 — SHA 불변 아카이브 + latest 포인터

```
gs://<CODE_ARTIFACTS_BUCKET>/code/<40자-commit-sha>.tar.gz   # 불변
gs://<CODE_ARTIFACTS_BUCKET>/code/latest.txt                  # 최신 main SHA
```

- SHA별 아카이브는 불변이다. 같은 SHA로 재실행하면 업로드를 생략한다(멱등).
- `latest.txt`는 tar.gz 복사본이 아니라 **40자 SHA 문자열 한 줄**을 담은
  텍스트 파일이다. 소비자는 `latest.txt`를 읽고 해당 SHA 아카이브를
  내려받으므로 어떤 버전을 실행했는지 항상 로그로 남는다.
- 재현·롤백: Airflow/runbook에서 특정 SHA를 직접 지정해 과거 버전을 실행할
  수 있다. latest가 잘못 가리키면 workflow_dispatch로 원하는 SHA를 다시
  가리키게 한다.

### 2. 아카이브 내용 — `git archive`로 전체 추적 파일

- `git archive --format=tar.gz <sha>`로 저장소 추적 파일 전체를 담는다.
- 미추적 파일(로컬 데이터, `.env`, `.venv` 등)은 자동 제외된다. 포함 목록을
  별도로 관리하지 않으므로 디렉토리가 추가되어도 스크립트 갱신이 불필요하다.
- 코드 저장소라 아카이브는 수 MB 수준이다.

### 3. 업로드 스크립트 — `scripts/upload_code_archive.sh`

로컬 수동 실행과 CI가 같은 경로를 타도록 로직을 스크립트 하나에 담고, CI는
호출만 한다.

**계약:**

- 환경 변수 `CODE_ARTIFACTS_BUCKET` (필수) — 대상 버킷 이름 (`gs://` 제외)
- 인자: git ref (기본 `HEAD`)
- `--update-latest` — 업로드 후 `latest.txt`를 이 SHA로 갱신. 플래그가
  없으면 아카이브만 올린다. 수동으로 과거 SHA를 올려도 latest가 오염되지
  않는다.
- `--dry-run` — gcloud 호출 없이 실행 계획만 출력한다. GCP 접근 없는 로컬
  검증용.

**동작 순서:**

1. `git rev-parse <ref>^{commit}`으로 40자 SHA 확정
2. `git archive --format=tar.gz -o <임시파일> <sha>`
3. `gcloud storage objects describe`로 동일 SHA 아카이브 존재 확인 →
   존재하면 업로드 생략
4. `gcloud storage cp`로 업로드 (gcloud 자체 체크섬 검증 사용)
5. `--update-latest`면 SHA를 담은 임시 파일을 `latest.txt`로 업로드
   (GCS object 쓰기는 원자적)
6. 결과 `gs://` 경로를 stdout에 출력

- `set -euo pipefail`, 필수 env 누락 시 명확한 메시지로 즉시 실패.

### 4. 워크플로우 — `.github/workflows/code-archive.yml`

- 트리거:
  - `push: branches: [main]` — 머지마다 자동 실행, `--update-latest` 포함
  - `workflow_dispatch` — SHA 입력(선택, 기본 main HEAD)과 latest 갱신 여부
    입력을 받아 수동 업로드·복구에 사용
- 경로 필터 없음. 모든 main push가 아카이브를 만들어 "latest == main HEAD"
  불변식을 유지한다. docs만 바뀐 머지도 아카이브가 생기지만 비용은 무시할
  수준이고, 불변식이 단순한 쪽을 우선한다.
- `concurrency: code-archive` (`cancel-in-progress: false`) — 동시 실행을
  막아 latest 포인터 경합을 방지한다. GitHub는 같은 그룹의 **대기 중** run을
  최신 것 하나만 남기므로, 짧은 간격의 연속 머지에서는 중간 SHA의 아카이브가
  생략될 수 있다. latest는 항상 최신 머지를 가리키므로 불변식에는 영향이
  없고, 생략된 SHA가 필요하면 workflow_dispatch로 보충한다.
- 인증: `google-github-actions/auth@v2` + `vars.WIF_PROVIDER_ID` +
  신규 secret `GCS_CODE_UPLOADER_SA` (release.yml의 WIF 패턴과 동일, SA만
  분리).
- checkout은 `fetch-depth: 0` (dispatch로 임의 SHA 아카이브 생성 지원).

### 5. 인프라 의존성 (Autoresearch-infra 요청)

이 저장소 구현은 GitHub secret/var만 참조하므로 인프라 작업과 병행 가능하다.

1. 코드 아티팩트 전용 버킷 생성 (제안: `<project>-code-artifacts`)
2. 업로더 SA 생성 + 해당 버킷 한정 `roles/storage.objectAdmin`
   (`latest.txt` 덮어쓰기에 delete 권한 필요) + GitHub Actions WIF 바인딩
3. (후속 이슈 시점) 파드 GSA에 해당 버킷 `roles/storage.objectViewer`

GitHub 저장소 설정: secret `CODE_ARTIFACTS_BUCKET`, `GCS_CODE_UPLOADER_SA`
등록.

### 6. 소비자 계약 (후속 이슈용)

파드 부트스트랩(Dockerfile.feast 전환 이슈)은 다음을 전제로 구현한다.

- `gs://<bucket>/code/latest.txt`에서 40자 SHA를 읽는다 (개행 trim).
- `gs://<bucket>/code/<sha>.tar.gz`를 내려받아 작업 디렉토리에 풀면 저장소
  루트 구조(`autoresearch/`, `feature_repo/`, `pyproject.toml`, ...)가
  그대로 나온다. tar 내부에 최상위 래핑 디렉토리는 없다.
- 특정 버전 고정 실행은 latest.txt를 건너뛰고 SHA를 env로 직접 받는다.
- 실행한 SHA를 파드 로그에 남긴다.

## 에러 처리

- 업로드 실패 시 워크플로우가 실패로 표시된다. main 코드 자체는 영향이
  없으며, 다음 머지 또는 workflow_dispatch 재실행으로 복구한다.
- 오래된 run 재실행으로 latest가 과거 SHA를 가리키게 되면
  workflow_dispatch로 최신 SHA를 다시 가리키게 한다.

## 검증

- `bash -n scripts/upload_code_archive.sh`, shellcheck(로컬에 있으면)
- `scripts/upload_code_archive.sh --dry-run`으로 GCP 접근 없이 동작 확인
- `git diff --check`, actionlint(로컬에 있으면)
- secret 등록 후 workflow_dispatch로 실업로드 end-to-end 확인 (secret 등록
  전에는 워크플로우가 인증 단계에서 실패하며, main 코드에는 영향이 없다)

## 범위 밖 (후속 이슈)

- `Dockerfile.feast`에서 코드 COPY 제거 + 부트스트랩 스크립트(다운로드 →
  압축 해제 → 커맨드 실행) 추가
- `Dockerfile.app`의 동일 방식 전환 여부 결정
- Airflow DAG에서 SHA 고정 실행 파라미터 전달 (`Autoresearch-airflow` 소관)
