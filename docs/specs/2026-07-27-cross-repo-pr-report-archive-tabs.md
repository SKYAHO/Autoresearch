# PR 리포트 아카이브 — Airflow 저장소 카테고리 탭

> Status: 설계 완료, 구현 예정 | Issue: #368 | Last Updated: 2026-07-27

## 배경과 목표

`SKYAHO/Autoresearch`는 merge된 PR 리포트를 모아 검색 가능한 정적
아카이브(`skyaho.github.io/Autoresearch/`, #348/#361)를 제공한다.
2026-07-27에 `SKYAHO/Autoresearch-airflow`에도 개별 PR 리포트 생성
워크플로우(`pr-report.yml`, Autoresearch-airflow#152/#153)를 이식했다.
이제 그 저장소의 merge PR도 같은 아카이브 화면에서 **카테고리를 골라**
모아 볼 수 있어야 한다.

범위는 이번엔 **application(Autoresearch) + airflow(Autoresearch-airflow)
두 카테고리**로 한정한다. infra(Autoresearch-infra)는 그 저장소에
PR 리포트·Pages 인프라 자체가 아직 없어 이번 범위에서 제외하고, 필요해지면
같은 패턴으로 추가한다(카테고리 하나 추가 = fetch 소스 하나 추가 수준).

## 범위

### 포함

- `Autoresearch-airflow`에 아카이브 생성기 3종(`build_archive.py`,
  `archive-template.html`, `archive.js`) + 워크플로우
  (`pr-report-archive.yml`)를 `Autoresearch`와 바이트 단위로 동일하게 이식
- `Autoresearch`의 `archive-template.html`에 카테고리 탭(전체·
  Application·Airflow) 추가
- `Autoresearch`의 `archive.js`에 airflow `archive.json`을 동일 origin
  fetch로 가져와 병합하는 로직 + 탭 필터 로직 추가
- 신규 순수함수 `matchesCategory(entry, category)` + node subprocess 기반
  단위 테스트(기존 `test_pr_report_archive_search.py` 패턴)

### 제외

- infra(Autoresearch-infra) 카테고리 — 별도 이슈
- `build_archive.py`/`pr-report-archive.yml`(Autoresearch) 변경 — 이번
  작업은 client-side 병합만 추가하고 서버 생성 로직은 건드리지 않는다
- `archive.json`/`report.schema.json` 스키마 변경 — category는 저장된
  데이터가 아니라 client가 fetch 출처로 판별해 붙이는 값이다
- 두 저장소 아카이브 생성 스케줄·트리거의 동기화 보장(각자 독립적으로
  갱신되며, 두 사이트의 `generated_at`이 다를 수 있음을 허용한다)

## 아키텍처

```text
Autoresearch(gh-pages)                 Autoresearch-airflow(gh-pages)
  index.html (inline application data)   index.html (inline airflow data)
  archive.json  ---------------------->  archive.json
  archive.js (fetch + merge + tabs)      archive.js (탭 없는 원본 그대로 — 이 사이트 자체는 단일 카테고리 뷰)
```

두 사이트는 `https://skyaho.github.io` **동일 origin**(경로만 다름)이므로
브라우저 `fetch()`에 CORS 제약이 없다. `Autoresearch`의 화면이 자신의
inline 데이터(application)와 `Autoresearch-airflow`의 정적
`archive.json`(airflow)을 **view 시점에** 병합해서 보여준다 — 두 저장소의
빌드 파이프라인은 서로 알지 못한 채 독립적으로 동작한다(장애 격리).

**작업 순서가 중요하다**: ① `Autoresearch-airflow`에 지금(탭 추가 전)의
`archive.js`/`archive-template.html`을 바이트 단위로 복사 → ② 그 다음에야
`Autoresearch` 쪽 사본에만 탭·cross-repo fetch 로직을 추가한다. 순서를
바꿔 탭 기능이 들어간 버전을 airflow에 복사하면, airflow 자기 사이트가
자기 자신을 다시 fetch하려는 잘못된 구조가 된다 — airflow의 사본은
이후로도 계속 원본(단일 카테고리) 상태로 남는다.

## 구성 요소

### `Autoresearch-airflow` 이식 (신규)

`.github/pr-report/{build_archive.py,archive-template.html,archive.js}`,
`.github/workflows/pr-report-archive.yml`을 `Autoresearch`에서 바이트
단위로 복사한다. 오늘 `pr-report.yml` 이식 때 확인한 것처럼 이 4개 파일에
저장소 이름 하드코딩이 없으므로(`${{ github.repository }}` 등으로 동적
참조) 무수정 복사로 충분할 것으로 예상하며, 구현 단계에서 diff로 재확인한다.

배포되면 오늘 bootstrap용으로 만든 placeholder `index.html`(gh-pages
루트)을 이 워크플로우의 첫 실행이 실제 아카이브 페이지로 덮어쓴다.

### `Autoresearch`의 `archive.js` 변경

- 카테고리 소스 설정을 코드 상단에 명시적으로 둔다:
  ```js
  var CATEGORY_SOURCES = [
    { key: "application", label: "Application" }, // inline #archive-data
    {
      key: "airflow",
      label: "Airflow",
      url: "https://skyaho.github.io/Autoresearch-airflow/archive.json",
      base: "https://skyaho.github.io/Autoresearch-airflow/",
    },
  ];
  ```
- 초기 렌더: inline 데이터(application)에 `category: "application"`을
  붙여 즉시 표시한다(cross-repo fetch를 기다리지 않음).
- `airflow` 소스는 `fetch()`(`AbortController`로 6초 타임아웃)로 비동기
  조회한다. 성공하면 각 entry에 `category: "airflow"`를 붙이고
  `report_url`을 `base + report_url`로 절대경로화한 뒤, 기존 목록과
  병합해 `merged_at` 내림차순으로 재정렬하고 재렌더링한다.
- 실패(네트워크 오류·타임아웃·JSON 파싱 실패)해도 application 목록은
  그대로 유지한다 — 콘솔 경고만 남기고 사용자에게는 airflow 탭 선택 시
  "이 카테고리를 불러오지 못했습니다" 안내만 보여준다.
- 신규 순수함수 `matchesCategory(entry, selectedKey)` —
  `selectedKey === "all" || entry.category === selectedKey`. 기존
  `matchesArchiveEntry`(텍스트 검색)와 AND 조건으로 함께 적용한다.
- 탭 클릭 시 `render()`를 카테고리+검색어 두 조건으로 다시 호출한다.
  탭 상태는 URL이나 로컬스토리지에 저장하지 않는다(새로고침 시 "전체"로
  리셋 — YAGNI, 필요해지면 추가).

### `Autoresearch`의 `archive-template.html` 변경

검색창 위에 탭 버튼 3개(전체·Application·Airflow)를 추가한다. 기존
색상 변수(`--accent` 등)를 재사용해 선택된 탭만 강조 스타일을 적용한다.
접근성을 위해 탭 그룹에 `role="tablist"`, 각 버튼에 `role="tab"`과
`aria-selected`를 사용한다.

## 데이터 계약

변경 없음. `archive.json`의 `schema_version: 1`,
`reports[].{number,title,author,merged_at,summary_ko,report_url}` 그대로
유지한다. `category`는 저장되지 않고 client가 fetch 출처로 부여하는
런타임 값이다. 두 저장소의 PR 번호가 우연히 같아도(`Autoresearch#153`과
`Autoresearch-airflow#153`처럼) `category`가 다르므로 목록에서 구분되고,
각 카드의 링크(`report_url`)도 서로 다른 절대/상대 경로를 가리키므로
충돌하지 않는다.

## 오류 처리

- airflow `archive.json` fetch 실패: application 카테고리는 정상 노출,
  airflow 탭 선택 시에만 안내 문구 표시. 페이지 전체가 깨지지 않는다.
- fetch 타임아웃(6초): 같은 실패 경로로 처리한다(무한 대기 방지).
- airflow entry에 `report_url`이 없거나 형식이 예상과 다르면 그 entry만
  건너뛰고 나머지는 정상 렌더링한다(단일 항목 오류가 전체를 막지 않음).

## 테스트

- `matchesCategory`: 기존 `test_pr_report_archive_search.py`의 node
  subprocess 패턴을 재사용해 `all`/`application`/`airflow` 케이스 검증
- 기존 `matchesArchiveEntry`·`build_archive.py` 테스트: 무변경이라 영향 없음
- cross-repo fetch·병합·렌더링(DOM 조작)은 node 환경에서 브라우저 API
  없이 단위 테스트하기 어려우므로, 구현 단계에서 실제 배포 후
  `skyaho.github.io/Autoresearch/`를 직접 열어 탭 전환·airflow 카드 표시·
  fetch 실패 시뮬레이션(airflow URL을 잠시 오타로 바꿔 확인 등)으로
  수동 검증한다.

## 관련

- 원본 아카이브 설계: `docs/specs/2026-07-26-pr-report-archive-design.md`
- 오늘 이식한 개별 리포트: `Autoresearch-airflow#152`/`#153`
