# Coding Conventions

> Last Updated: 2026-07-06

구현 세부 사항에 대한 실용 컨벤션입니다. 필수 규칙은 `CLAUDE.md`에
있습니다.

## Naming

- 도메인 개념과 연결된 서술적인 이름을 사용합니다.
- 함수 이름은 동작 중심으로 짓습니다 (`fetch_trending_videos`,
  `transform_raw_items`).
- 이미 저장소에 정착된 경우가 아니면 축약어를 피합니다.
- 모듈 이름은 단계를 드러냅니다 (`fetch.py`, `transform.py`,
  `load.py`, `backfill.py`, `schema.py`).

## Module Boundaries

- **fetch:** 외부 API 호출과 원본 응답 수집만 담당합니다.
- **transform:** 원본 → 정제 데이터 변환과 스키마 검증을 담당합니다.
- **load:** GCS 등 외부 저장소 적재 의미론을 담당합니다.
- **schema:** pydantic 모델로 모듈 간 데이터 계약을 정의합니다.
- **pipeline:** 단계들을 엮는 오케스트레이션만 담당합니다.
- **dags/:** Airflow DAG 정의만 담습니다. 비즈니스 로직은
  `autoresearch/` 모듈에 둡니다.

## Imports

- `autoresearch` 기준 절대 import를 사용합니다.
- 표준 라이브러리 → 서드파티 → 로컬 순으로 정렬합니다.
- 정해진 진입점 밖에서 import 시점 부수효과를 만들지 않습니다.

## Comments

- 비자명한 제약이나 트레이드오프를 설명할 때만 주석을 답니다.
- 자명한 대입이나 흐름을 서술하지 않습니다.
- 공개 헬퍼와 복잡한 함수에는 docstring을 우선합니다.
- 호출 계약이 자명하지 않은 경우 Google 스타일 docstring을 씁니다:
  - `Args:` 의미 있는 파라미터 (공개 API, 모듈 경계, DAG에서 호출되는
    함수)
  - `Returns:` 함수 이름이나 타입만으로 반환값이 자명하지 않을 때
  - `Raises:` 검증 실패, 외부 경계 오류 등 호출자가 처리해야 하는
    예외
- docstring은 간결하게, 동작 수준으로 유지합니다. 자명한 private
  헬퍼에 커버리지용 docstring을 달지 않습니다.

## Data Handling

- 모듈 간 데이터 계약 변경은 `schema.py`의 pydantic 모델과 테스트를
  함께 수정합니다.
- 스키마 필드 추가·삭제는 하위 소비자(변환, 적재, DAG)에 미치는
  영향을 확인합니다.
- 생성된 데이터 파일(`*.csv`, `*.pkl`, parquet 산출물)은 커밋하지
  않습니다.
- GCS 경로와 버킷 이름은 환경 변수로 주입합니다.

## Dependency Changes

- 실제 복잡도를 줄여줄 때만 의존성을 추가합니다.
- 런타임 의존성은 `requirements.txt`, 개발·실험 의존성은
  `requirements-dev.txt`에 추가합니다. `requirements.txt`는 Astro
  이미지와 CI 이미지가 공유하는 단일 출처이므로 런타임에 불필요한
  패키지를 넣지 않습니다.
- 사용자가 설치하거나 설정해야 하는 운영 의존성은 문서화합니다.
