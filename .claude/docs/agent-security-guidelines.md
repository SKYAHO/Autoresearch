# Agent Security Guidelines

> Last Updated: 2026-07-06

시크릿, 자격 증명, 외부 입력을 다룰 때 사용하는 문서입니다.

## Secrets

- 다음을 절대 커밋하지 않습니다:
  - `.env` (로컬 환경 변수)
  - `keys/` 하위 파일, `.gcp-creds.json` (GCP 자격 증명)
  - API 키, 토큰, 서비스 계정 JSON
- 시크릿은 환경 변수로만 주입합니다. 로컬은 `.env`, CI/CD는 GitHub
  Secrets를 사용합니다.
- 새 시크릿이 필요하면 값이 비어 있는 항목을 `.env.example`에
  추가하고 용도를 주석으로 남깁니다.
- 커밋 전 `git status`와 diff에서 시크릿·자격 증명 파일이 포함되지
  않았는지 확인합니다.

## Logging and Error Messages

- API 키, 자격 증명, 서명된 URL을 로그나 에러 메시지에 남기지
  않습니다.
- 외부 서비스 실패를 보고할 때는 작업 이름과 정제된 엔드포인트만
  포함합니다.
- 전체 환경 변수 덤프를 출력하지 않습니다.

## External Input

- 외부에서 들어오는 데이터는 신뢰하지 않고 pydantic 스키마로 검증한
  뒤 사용합니다:
  - YouTube Data API 응답
  - Kaggle 등 외부 parquet/CSV 원천
  - Gemini API 응답
- 외부 입력으로 파일 경로나 GCS 경로를 조립할 때는 예상 범위를
  벗어나지 않는지 확인합니다.

## GitHub Actions

- workflow의 `permissions`는 필요한 최소 권한만 부여합니다.
- 시크릿은 GitHub Secrets로 참조하고 workflow 파일에 하드코딩하지
  않습니다.
- 외부에서 제어 가능한 입력(PR 제목, 코멘트 본문)을 shell에 직접
  보간하지 않습니다.
- workflow 변경 시 `git diff --check`를 실행하고, 로컬에 `actionlint`가
  있으면 함께 사용합니다.

## GCP

- 서비스 계정에는 최소 권한 원칙을 적용합니다 (버킷 단위 권한 우선).
- 버킷 이름과 프로젝트 ID는 환경 변수로 관리합니다.
- 로컬 개발용 자격 증명과 프로덕션 자격 증명을 분리합니다.
