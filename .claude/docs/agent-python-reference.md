# Agent Python Reference

> Last Updated: 2026-07-06

Python 코드를 작성하거나 수정할 때 사용하는 문서입니다.

## Pre-Implementation Checklist

1. 새 함수와 변경된 함수에 반환 타입을 포함한 타입 힌트를 답니다.
2. 호출 계약이 자명하지 않은 공개 헬퍼와 모듈 경계 함수에 Google
   스타일 docstring을 답니다.
3. 설정 값은 환경 변수로 받고, 사용자에게 노출되는 값이면
   `.env.example`에 문서화합니다.
4. 모듈 간 데이터는 `schema.py`의 pydantic 모델로 검증합니다.
5. 테스트에 임시 디렉터리나 환경 변수 격리가 필요한지 확인합니다.
6. Python 3.11과 3.12 양쪽에서 동작해야 합니다 (CI 매트릭스).

## Typing

- 함수에는 반환 타입을 포함한 타입 힌트가 필수입니다.
- 이미 존재하는 구체적인 도메인 타입(pydantic 모델)을 우선합니다.
- 파일 경로는 가능하면 raw 문자열 대신 `Path`를 사용합니다.
- 외부 API가 강제하는 경우가 아니면 `Any`로 타입을 넓히지 않습니다.

## Data Validation

- 외부에서 들어오는 데이터(YouTube API 응답, Kaggle parquet, Gemini
  응답)는 pydantic 모델로 검증한 뒤 사용합니다.
- pydantic v2 문법을 사용합니다 (`model_validate`, `model_dump`).
- 스키마 변경 시 대응하는 테스트를 함께 갱신합니다.

## Configuration

- 설정은 환경 변수로 주입합니다 (`YOUTUBE_API_KEYS` (복수, 쉼표 구분),
  `YOUTUBE_PROXY_URL`, `YOUTUBE_LAKE_BUCKET`, `YOUTUBE_BACKFILL_SOURCE` 등).
- 기본값은 로컬 개발에 안전한 값으로 둡니다.
- 새 환경 변수를 추가하면 `.env.example`과 필요 시
  `airflow_settings.yaml`을 갱신합니다.
- Airflow 환경에서는 Airflow Variables로도 주입될 수 있음을
  고려합니다.

## Logging and Errors

- 시크릿, API 키, 토큰, 전체 환경 변수 덤프를 로그에 남기지 않습니다.
- 실패한 작업과 안전한 컨텍스트를 알 수 있는 명확한 에러 메시지를
  씁니다.
- 기존 예외 타입과 동작을 유지합니다. 상세는
  `agent-error-handling-reference.md`를 참조합니다.

## Tests

- 테스트는 `tests/test_<module>.py`에 배치합니다.
- 변경된 동작에 집중된 테스트를 우선합니다.
- 외부 API(YouTube, GCS, Gemini)는 mock으로 격리합니다. 실제
  네트워크 호출을 테스트에 넣지 않습니다.
- 파일 산출물은 `tmp_path` 등 임시 디렉터리를 사용합니다.
- 실행: `python -m pytest -v` (CI와 동일)
