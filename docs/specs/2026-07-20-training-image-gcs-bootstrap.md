# 학습 이미지(Dockerfile.train) GCS 코드 부트스트랩 전환

- **상태**: Approved
- **날짜**: 2026-07-20
- **이슈**: #177
- **관련 문서**:
  - `docs/specs/2026-07-18-feast-bootstrap-gcs-code.md` (#181 — 원형 구현)
  - `docs/specs/2026-07-18-code-archive-gcs-upload.md` (#174 — 코드 아카이브 업로드 계약)
  - `docs/specs/2026-07-13-public-batch-execution-contract.md`

## 목적

`Dockerfile.train`은 `COPY src ./src`로 코드를 이미지 빌드 시점에 baking한다.
코드가 바뀌어도 이미지를 재빌드하지 않으면 학습 Pod는 옛 코드로 계속
실행된다. `Dockerfile.feast`(#181)가 이미 구현한 "실행 환경(이미지)과 코드
분리" 패턴을 학습 이미지에도 적용해, 이미지 재빌드는 의존성 변경 시에만
필요하도록 전환한다.

## 결정

### 1. 부트스트랩 스크립트를 범용 이름으로 통합

`scripts/feast_bootstrap.sh`를 `scripts/gcs_code_bootstrap.sh`로 이름을
바꾼다. 스크립트 내부 로직은 Feast 전용 코드가 전혀 없었다(GCS 코드
아카이브를 받아 `/app`에 풀고 `exec "$@"`하는 범용 동작) — 이름만
"feast"였을 뿐이라, `Dockerfile.feast`·`Dockerfile.train` 양쪽의 공용
ENTRYPOINT로 재사용한다. 새 스크립트를 이미지별로 복제하지 않는다(구조
변경 없이 동일 스크립트를 두 곳에서 참조).

로그 프리픽스도 `[feast-bootstrap]` → `[gcs-bootstrap]`으로 범용화했다.
사용법(usage) 메시지의 스크립트 이름도 함께 갱신했다.

### 2. `Dockerfile.train` 변경

`Dockerfile.feast`와 동일한 패턴을 멀티스테이지 빌드에 맞게 적용한다:

- `COPY src ./src` 제거
- `COPY scripts/gcs_code_bootstrap.sh /usr/local/bin/gcs_code_bootstrap.sh`
- `ENTRYPOINT ["/usr/local/bin/gcs_code_bootstrap.sh"]`, 기존
  `CMD ["python", "-m", "src.cli", "--help"]` 유지 (부트스트랩이 코드를
  푼 뒤 이 CMD가 여전히 유효한 스모크로 동작)
- `RUN chown appuser /app` — `Dockerfile.feast`는 `chown -R`을 쓰지만,
  `Dockerfile.train`의 최종 스테이지는 `pyproject.toml`/`uv.lock`을 `/app`에
  COPY하지 않는다(builder 스테이지에서만 bind mount로 사용). 즉 아카이브가
  덮어써야 할 기존 root 소유 파일이 없고, `/app` 디렉토리 자체에 새 엔트리를
  만들 쓰기 권한만 있으면 된다 — venv 전체를 재귀적으로 chown할 필요가 없어
  `-R`을 빼고 빌드 비용을 줄였다.

### 3. `.gitattributes` 추가 (이번 전환 중 발견된 선행 결함 수정)

로컬 검증 중 `scripts/*.sh`가 Windows(`core.autocrlf=true`) 환경에서
CRLF로 checkout되면 `gcs_code_bootstrap.sh`의 셔뱅(`#!/usr/bin/env bash`)이
깨져 컨테이너 안에서 `env: 'bash\r': No such file or directory`로 실패하는
것을 확인했다(`scripts/upload_code_archive.sh`도 동일하게 이미 영향받고
있었음 — 이번 전환과 무관한 기존 파일). CI는 리눅스 러너라 지금까지
드러나지 않았을 뿐이다. `*.sh text eol=lf`를 저장소 루트에 추가해 OS와
무관하게 셸 스크립트가 항상 LF로 checkout되도록 고정했다.

### 4. `ci.yml` 학습 이미지 스모크 체크 변경

기존 `Run training image smoke check` 스텝을 `Dockerfile.feast`와 동일한
로컬 아카이브 주입 모드로 전환한다:

1. `git archive --format=tar.gz -o /tmp/code-archive.tar.gz HEAD`
2. 각 `docker run`에 `-v /tmp/code-archive.tar.gz:/tmp/code-archive.tar.gz:ro
   -e CODE_ARCHIVE_LOCAL_PATH=/tmp/code-archive.tar.gz` 추가
3. 기존 네 검증(기본 CMD, `build-features --help`, `train-model --help`,
   `evaluate-model --help`, `run-pipeline --help`) 유지
4. env 없이 실행 시 실패해야 한다는 회귀 검증 추가(Feast와 동일)

## 로컬 실측 검증

Windows 로컬에서 `docker build -f Dockerfile.train`으로 빌드 후, 로컬
아카이브 주입 모드로 실제 확인했다:

- env 없이 실행 → `오류: CODE_ARTIFACTS_BUCKET 또는 CODE_ARCHIVE_LOCAL_PATH
  환경 변수가 필요합니다` (exit 2) — 부트스트랩 없이는 절대 성공하지 않음
- `CODE_ARCHIVE_LOCAL_PATH`로 로컬 아카이브 주입 → `[gcs-bootstrap] code:
  local:...` 로그 출력 후 `python -m src.cli`, `train-model --help`,
  `run-pipeline --help` 전부 정상 동작 확인(코드가 `/app`에 풀려있어야만
  가능한 경로)

`Dockerfile.feast`는 스크립트 rename 후 재빌드를 시도했으나 feast 그룹
의존성 설치가 로컬 환경에서 5분 이상 걸려 타임아웃됨(이번 변경과 무관한
기존 설치 소요 시간) — 변경 내용 자체는 파일 경로 두 줄 치환뿐이고, 참조하는
스크립트는 `Dockerfile.train` 경로로 이미 실측 검증됐다. CI가 최종 확인한다.

## 경계·의존성 — 이 PR은 3개 저장소 중 Autoresearch만 다룬다

**중요**: 이 스펙만으로 학습 이미지를 실제 운영에 배포하면 안 된다.
`Autoresearch-airflow`의 `AutoresearchBatchPodOperator`가 여전히
`cmds=["python", "-m", module]`로 K8s `command`를 설정하면 이미지의
`ENTRYPOINT`(부트스트랩)를 완전히 덮어써서 부트스트랩이 아예 실행되지
않는다 — Pod는 코드가 하나도 없는 상태에서 곧장
`ModuleNotFoundError`로 죽는다. 실제로 이미 이 문제가 발생 중이다:
`Autoresearch-airflow`에 최근 추가된 `feast_online_store_materialize`
DAG(스케줄 `0 0 * * *`)가 `Dockerfile.feast`의 부트스트랩 이미지를 쓰는데,
오퍼레이터가 여전히 `cmds`를 설정해 이 DAG는 현재 매일 실패하고 있을
것으로 추정된다(코드만으로 확인, 실제 Pod 로그로 재현 확인은 하지 않음).

- **Autoresearch (이 PR)**: 이미지·스크립트·CI만 변경. 학습 이미지는
  빌드는 되지만, 오퍼레이터가 고쳐지기 전까지 `ctr_model_training` DAG에
  실제로 배포(Variable digest 갱신)하면 안 된다.
- **Autoresearch-airflow (별도 PR)**: `AutoresearchBatchPodOperator`가
  `cmds` 대신 `arguments`만으로 `["python", "-m", module, *arguments]`를
  전달하도록 변경(이미지 `ENTRYPOINT`를 항상 보존) + `ctr_training` DAG에
  `CODE_ARTIFACTS_BUCKET` env 배선 추가. 이 변경은 `feast_materialize` DAG의
  현재 추정 장애도 함께 고친다.
- **Autoresearch-infra (별도 PR, 코드만 — apply는 요청)**:
  `autoresearch-batch` GSA에 코드 아카이브 버킷
  `roles/storage.objectViewer` 바인딩 추가.

## 범위 밖

- `Dockerfile.app`의 동일 방식 전환 여부 — `docs/specs/2026-07-18-feast-bootstrap-gcs-code.md`에서도
  범위 밖으로 명시됐고 아직 미결정. `youtube_backfill`/`youtube_gcs_action_log`
  DAG가 이 이미지를 쓰며, 오퍼레이터 변경(`arguments`만 사용)은 이 이미지들이
  `ENTRYPOINT`를 정의하지 않은 현재 상태에서도 동일하게 동작하도록 설계했다
  (K8s가 `command` 없이 `args`만 받으면 이미지의 기본 ENTRYPOINT를 쓰는데,
  `Dockerfile.app`은 ENTRYPOINT가 없어 `args`가 그대로 실행된다 — 기존
  동작과 동일).
- Airflow DAG env 배선, GSA IAM 바인딩 — 각각 `Autoresearch-airflow`,
  `Autoresearch-infra` PR에서 다룬다.
