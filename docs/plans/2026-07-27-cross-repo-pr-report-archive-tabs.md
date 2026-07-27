# PR 리포트 아카이브 — Airflow 카테고리 탭 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `skyaho.github.io/Autoresearch/` 아카이브 화면에서 Autoresearch(application)와
Autoresearch-airflow(airflow) 두 저장소의 merge PR 리포트를 카테고리 탭으로 골라 볼 수
있게 만든다.

**Architecture:** `Autoresearch-airflow`에 기존 아카이브 생성기(`build_archive.py`,
`archive-template.html`, `archive.js`, `pr-report-archive.yml`)를 탭 기능 추가 전
버전 그대로 이식해 자체 `archive.json`을 생성하게 한다. `Autoresearch`의
`archive.js`만 이 정적 JSON을 동일 origin(`https://skyaho.github.io`) fetch로
가져와 자신의 inline 데이터와 client-side에서 병합하고, 탭 UI로 필터링한다.
서버 생성 로직(`build_archive.py`)은 두 저장소 모두 무변경.

**Tech Stack:** GitHub Actions, Python 3(`build_archive.py`, 무변경), 순수 JS(ES2017+,
`archive.js`), GitHub Pages(gh-pages 브랜치), pytest + Node.js subprocess 테스트 패턴.

## Global Constraints

- 두 사이트는 `https://skyaho.github.io`로 동일 origin(경로만 다름) — fetch에
  CORS 문제 없음. spec: `docs/specs/2026-07-27-cross-repo-pr-report-archive-tabs.md`
- `category`는 저장된 데이터가 아니라 client가 fetch 출처로 부여하는 런타임 값 —
  `archive.json` 스키마(`schema_version: 1`)는 두 저장소 모두 변경하지 않는다.
- **순서가 중요하다**: Autoresearch-airflow에는 탭 기능 추가 **전**(현재) 버전의
  `archive.js`/`archive-template.html`을 먼저 이식하고, 탭·fetch 병합 로직은
  이후 Autoresearch 쪽 사본에만 추가한다. 순서를 바꾸면 airflow 사이트가 자기
  자신을 다시 fetch하려는 잘못된 구조가 된다.
- airflow `archive.json`을 가져오는 fetch는 6초(`FETCH_TIMEOUT_MS = 6000`)
  타임아웃을 두고, 실패해도 application 카테고리는 정상 노출한다(장애 격리).
- 병합 정렬은 `build_archive.py`의 기존 규칙과 동일하게 `merged_at` 내림차순,
  동률이면 `number` 내림차순.
- 이 저장소(Autoresearch)의 테스트는 Node.js subprocess로 `archive.js`의
  `module.exports` 순수함수만 검증한다(기존 `tests/test_pr_report_archive_search.py`
  패턴). DOM 렌더링·실제 fetch 통합은 자동 테스트 대상이 아니며 배포 후 수동
  검증한다(brainstormed 합의 사항).
- 코딩 스타일은 기존 `archive.js`를 따른다: `"use strict"`, `var` 선언,
  함수 선언식, ES5 스타일 콜백(화살표 함수·`const`/`let` 미사용) — 기존 코드와
  섞이지 않게 일관성 유지.

---

### Task 1: Autoresearch-airflow에 아카이브 생성기 이식 (탭 추가 전 버전)

**Working directory:** `/home/yjlee/Autoresearch-airflow` (별도 git 저장소)

**Files:**
- Create: `/home/yjlee/Autoresearch-airflow/.github/pr-report/build_archive.py`
- Create: `/home/yjlee/Autoresearch-airflow/.github/pr-report/archive-template.html`
- Create: `/home/yjlee/Autoresearch-airflow/.github/pr-report/archive.js`
- Create: `/home/yjlee/Autoresearch-airflow/.github/workflows/pr-report-archive.yml`
- 원본: `/home/yjlee/Autoresearch`의 **origin/main 현재 버전**(Task 2~4 적용 전)
  같은 경로

**Interfaces:**
- Produces: `https://skyaho.github.io/Autoresearch-airflow/archive.json`
  (`schema_version: 1`, `reports[].{number,title,author,merged_at,summary_ko,report_url}`),
  Task 4에서 Autoresearch 쪽 `CATEGORY_SOURCES`가 이 URL을 fetch 대상으로 사용한다.

- [ ] **Step 1: 이슈 발행 + 브랜치 생성**

```bash
cat <<'EOF' > /tmp/issue-archive-port.md
## 배경

`SKYAHO/Autoresearch`는 merge된 PR 리포트를 모아 검색 가능한 정적
아카이브(`skyaho.github.io/Autoresearch/`, #348/#361)를 제공한다. 오늘
개별 PR 리포트 생성(`pr-report.yml`, Autoresearch-airflow#152/#153)에
이어, merge PR을 모아 보여주는 아카이브 생성기도 이 저장소에 동일하게
이식한다.

## 변경 내용

- `.github/pr-report/{build_archive.py,archive-template.html,archive.js}`,
  `.github/workflows/pr-report-archive.yml`을 Autoresearch에서 바이트
  단위로 동일하게 이식(탭 기능 없는 원본 버전)

## 관련

- Autoresearch#368 (Autoresearch 쪽 탭 추가 작업, 이 이슈가 만드는
  `archive.json`을 그쪽에서 fetch한다)
- 원본 설계: Autoresearch `docs/specs/2026-07-27-cross-repo-pr-report-archive-tabs.md`
EOF
gh issue create --repo SKYAHO/Autoresearch-airflow \
  --title "[FEAT] Autoresearch의 merge PR 아카이브 워크플로우 이식" \
  --body-file /tmp/issue-archive-port.md
```

출력된 이슈 번호를 `<N>`이라 하고:

```bash
cd /home/yjlee/Autoresearch-airflow
gh issue develop <N> --repo SKYAHO/Autoresearch-airflow --checkout
```

- [ ] **Step 2: 4개 파일을 Autoresearch 현재 버전에서 그대로 복사**

```bash
mkdir -p /home/yjlee/Autoresearch-airflow/.github/pr-report
cp /home/yjlee/Autoresearch/.github/pr-report/build_archive.py \
   /home/yjlee/Autoresearch-airflow/.github/pr-report/build_archive.py
cp /home/yjlee/Autoresearch/.github/pr-report/archive-template.html \
   /home/yjlee/Autoresearch-airflow/.github/pr-report/archive-template.html
cp /home/yjlee/Autoresearch/.github/pr-report/archive.js \
   /home/yjlee/Autoresearch-airflow/.github/pr-report/archive.js
cp /home/yjlee/Autoresearch/.github/workflows/pr-report-archive.yml \
   /home/yjlee/Autoresearch-airflow/.github/workflows/pr-report-archive.yml
```

**주의**: 이 Step은 Task 2~4를 실행하기 **전**에 끝나 있어야 한다(먼저 이
Task를 완료하고 커밋·푸시까지 마친 뒤에 Task 2로 넘어간다). 순서를
바꾸면 airflow 쪽에 탭 기능이 들어간 버전이 복사되어 Global Constraints의
경고 상황이 발생한다.

- [ ] **Step 3: 저장소 이름 하드코딩 여부 재확인**

```bash
grep -n "Autoresearch\b" /home/yjlee/Autoresearch-airflow/.github/pr-report/build_archive.py \
  /home/yjlee/Autoresearch-airflow/.github/pr-report/archive-template.html \
  /home/yjlee/Autoresearch-airflow/.github/pr-report/archive.js \
  /home/yjlee/Autoresearch-airflow/.github/workflows/pr-report-archive.yml
```

Expected: 아무 결과도 없음(전부 `${{ github.repository }}` 등으로 동적
참조). 만약 결과가 있으면 그 줄만 이 저장소 이름에 맞게 고친다.

- [ ] **Step 4: 문법 검증**

```bash
cd /home/yjlee/Autoresearch-airflow
python3 -m py_compile .github/pr-report/build_archive.py && echo "build_archive.py OK"
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/pr-report-archive.yml'))" && echo "workflow yaml OK"
grep -q "__ARCHIVE_DATA__" .github/pr-report/archive-template.html && echo "template placeholder OK"
```

Expected: 3줄 모두 `OK` 출력.

- [ ] **Step 5: 커밋·푸시·PR**

```bash
cd /home/yjlee/Autoresearch-airflow
git add .github/pr-report/build_archive.py .github/pr-report/archive-template.html \
  .github/pr-report/archive.js .github/workflows/pr-report-archive.yml
git commit -m "feat: Autoresearch의 merge PR 아카이브 워크플로우 이식 (#<N>)

pr-report.yml(#152/#153)에 이어 merge PR을 모아 보여주는 아카이브
생성기(build_archive.py, archive-template.html, archive.js,
pr-report-archive.yml)를 바이트 단위로 동일하게 이식한다. 탭 기능은
Autoresearch 쪽 사본에만 추가되며 이 저장소의 사본은 원본(단일 카테고리)
그대로 유지한다.

Refs #<N>"
git push -u origin <branch-name>
gh pr create --repo SKYAHO/Autoresearch-airflow \
  --title "feat: Autoresearch의 merge PR 아카이브 워크플로우 이식 (#<N>)" \
  --body "Closes #<N>. Autoresearch \`docs/specs/2026-07-27-cross-repo-pr-report-archive-tabs.md\` 참고."
```

- [ ] **Step 6: (이 Task의 배포 검증은 Task 5로 미룬다 — 지금은 PR만 올려둔다)**

이 PR은 병합 전까지 워크플로우가 실행되지 않는다(트리거가
`pull_request: closed(merged)` / `workflow_run` / `workflow_dispatch`).
Task 5에서 병합 + `workflow_dispatch`로 첫 실행을 트리거하고 결과를
검증한다.

---

### Task 2: `matchesCategory` 순수함수 (TDD)

**Working directory:** `/home/yjlee/Autoresearch`

**Files:**
- Modify: `.github/pr-report/archive.js`
- Test: `tests/test_pr_report_archive_category.py`

**Interfaces:**
- Consumes: 없음(신규 독립 함수)
- Produces: `matchesCategory(entry, selectedKey) -> boolean` — Task 4의
  `render()`가 `matchesArchiveEntry`와 AND 조건으로 사용한다.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_pr_report_archive_category.py` 생성:

```python
from __future__ import annotations

import json
import subprocess
from pathlib import Path

ARCHIVE_JS = (
    Path(__file__).resolve().parents[1] / ".github" / "pr-report" / "archive.js"
)


def _matches_category(entry: dict, category: str) -> bool:
    script = (
        f"const archive=require({json.dumps(str(ARCHIVE_JS))});"
        "process.stdout.write(String(archive.matchesCategory("
        f"{json.dumps(entry, ensure_ascii=False)},"
        f"{json.dumps(category, ensure_ascii=False)})));"
    )
    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout == "true"


def test_all_category_matches_every_entry():
    assert _matches_category({"category": "application"}, "all")
    assert _matches_category({"category": "airflow"}, "all")


def test_specific_category_matches_only_itself():
    assert _matches_category({"category": "application"}, "application")
    assert not _matches_category({"category": "airflow"}, "application")
    assert _matches_category({"category": "airflow"}, "airflow")
    assert not _matches_category({"category": "application"}, "airflow")
```

- [ ] **Step 2: 실패 확인**

Run: `cd /home/yjlee/Autoresearch && uv run python -m pytest tests/test_pr_report_archive_category.py -v`
Expected: FAIL — `subprocess.CalledProcessError`(`archive.matchesCategory is not a function`),
`matchesCategory`가 아직 `module.exports`에 없기 때문.

- [ ] **Step 3: 최소 구현**

`.github/pr-report/archive.js`에서 `matchesArchiveEntry` 함수 바로 아래에 추가:

```js
function matchesCategory(entry, selectedKey) {
  return selectedKey === "all" || entry.category === selectedKey;
}
```

파일 맨 아래 export 블록을 다음으로 교체:

```js
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    matchesArchiveEntry: matchesArchiveEntry,
    matchesCategory: matchesCategory,
  };
}
```

- [ ] **Step 4: 통과 확인**

Run: `cd /home/yjlee/Autoresearch && uv run python -m pytest tests/test_pr_report_archive_category.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: 커밋**

```bash
git add .github/pr-report/archive.js tests/test_pr_report_archive_category.py
git commit -m "feat: archive.js에 matchesCategory 필터 함수 추가

카테고리 탭 필터링에 쓸 순수함수. tests/test_pr_report_archive_search.py와
동일한 node subprocess 패턴으로 테스트한다."
```

---

### Task 3: 병합·태깅 순수함수 3종 (TDD)

**Working directory:** `/home/yjlee/Autoresearch`

**Files:**
- Modify: `.github/pr-report/archive.js`
- Test: `tests/test_pr_report_archive_merge.py`

**Interfaces:**
- Consumes: 없음(신규 독립 함수 3개)
- Produces:
  - `tagCategory(entries, categoryKey) -> Array` — 각 entry를 얕은 복사하고
    `category` 필드를 추가한 새 배열을 반환(입력 배열/객체는 변경하지 않음)
  - `absolutizeReportUrl(entries, baseUrl) -> Array` — 각 entry의
    `report_url`을 `baseUrl + report_url`로 교체한 새 배열을 반환
  - `mergeAndSortReports(reportArrays) -> Array` — 여러 배열을 합쳐
    `merged_at` 내림차순(동률이면 `number` 내림차순)으로 정렬
  - Task 4의 `initializeArchive()`가 이 3개를 그대로 사용한다.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_pr_report_archive_merge.py` 생성:

```python
from __future__ import annotations

import json
import subprocess
from pathlib import Path

ARCHIVE_JS = (
    Path(__file__).resolve().parents[1] / ".github" / "pr-report" / "archive.js"
)


def _call(function_name: str, *args: object) -> object:
    args_json = ",".join(json.dumps(arg, ensure_ascii=False) for arg in args)
    script = (
        f"const archive=require({json.dumps(str(ARCHIVE_JS))});"
        f"process.stdout.write(JSON.stringify(archive.{function_name}({args_json})));"
    )
    result = subprocess.run(
        ["node", "-e", script],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_tag_category_adds_field_without_mutating_input():
    entries = [{"number": 1}, {"number": 2}]
    tagged = _call("tagCategory", entries, "airflow")
    assert tagged == [
        {"number": 1, "category": "airflow"},
        {"number": 2, "category": "airflow"},
    ]


def test_absolutize_report_url_prefixes_base():
    entries = [{"report_url": "pr/153/"}]
    absolute = _call(
        "absolutizeReportUrl",
        entries,
        "https://skyaho.github.io/Autoresearch-airflow/",
    )
    assert absolute == [
        {"report_url": "https://skyaho.github.io/Autoresearch-airflow/pr/153/"}
    ]


def test_merge_and_sort_reports_orders_by_merged_at_desc_then_number_desc():
    application = [{"number": 10, "merged_at": "2026-07-20T00:00:00Z"}]
    airflow = [
        {"number": 5, "merged_at": "2026-07-26T00:00:00Z"},
        {"number": 4, "merged_at": "2026-07-20T00:00:00Z"},
    ]
    merged = _call("mergeAndSortReports", [application, airflow])
    assert [entry["number"] for entry in merged] == [5, 10, 4]
```

- [ ] **Step 2: 실패 확인**

Run: `cd /home/yjlee/Autoresearch && uv run python -m pytest tests/test_pr_report_archive_merge.py -v`
Expected: FAIL — `CalledProcessError`(`archive.tagCategory is not a function` 등)

- [ ] **Step 3: 최소 구현**

`.github/pr-report/archive.js`에서 `matchesCategory` 함수 바로 아래에 추가:

```js
function shallowCopy(entry) {
  var copy = {};
  for (var key in entry) {
    if (Object.prototype.hasOwnProperty.call(entry, key)) copy[key] = entry[key];
  }
  return copy;
}

function tagCategory(entries, categoryKey) {
  return entries.map(function (entry) {
    var tagged = shallowCopy(entry);
    tagged.category = categoryKey;
    return tagged;
  });
}

function absolutizeReportUrl(entries, baseUrl) {
  return entries.map(function (entry) {
    var absolute = shallowCopy(entry);
    absolute.report_url = baseUrl + entry.report_url;
    return absolute;
  });
}

function mergeAndSortReports(reportArrays) {
  var merged = [].concat.apply([], reportArrays);
  return merged.slice().sort(function (a, b) {
    if (a.merged_at === b.merged_at) return b.number - a.number;
    return a.merged_at < b.merged_at ? 1 : -1;
  });
}
```

export 블록을 다음으로 교체:

```js
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    matchesArchiveEntry: matchesArchiveEntry,
    matchesCategory: matchesCategory,
    tagCategory: tagCategory,
    absolutizeReportUrl: absolutizeReportUrl,
    mergeAndSortReports: mergeAndSortReports,
  };
}
```

- [ ] **Step 4: 통과 확인**

Run: `cd /home/yjlee/Autoresearch && uv run python -m pytest tests/test_pr_report_archive_merge.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: 커밋**

```bash
git add .github/pr-report/archive.js tests/test_pr_report_archive_merge.py
git commit -m "feat: archive.js에 카테고리 태깅·URL 절대화·병합정렬 순수함수 추가

tagCategory/absolutizeReportUrl/mergeAndSortReports — cross-repo fetch
결과를 application 데이터와 합치는 데 쓴다. 정렬 규칙은 build_archive.py의
merged_at desc, number desc 동률처리와 동일하게 맞춘다."
```

---

### Task 4: 탭 UI + cross-repo fetch 배선

**Working directory:** `/home/yjlee/Autoresearch`

**Files:**
- Modify: `.github/pr-report/archive.js`
- Modify: `.github/pr-report/archive-template.html`

**Interfaces:**
- Consumes: `matchesCategory`(Task 2), `tagCategory`/`absolutizeReportUrl`/
  `mergeAndSortReports`(Task 3) — 전부 이 파일 안의 로컬 함수라 import 불필요.
- Produces: 브라우저에서 동작하는 완성된 기능(자동 테스트 대상 아님, Step 4에서
  수동 검증).

- [ ] **Step 1: `archive.js`에 카테고리 소스 설정과 라벨 맵 추가**

`"use strict";` 바로 아래, `matchesArchiveEntry` 함수 위에 추가:

```js
var CATEGORY_SOURCES = [
  { key: "application", label: "Application" },
  {
    key: "airflow",
    label: "Airflow",
    url: "https://skyaho.github.io/Autoresearch-airflow/archive.json",
    base: "https://skyaho.github.io/Autoresearch-airflow/",
  },
];
var FETCH_TIMEOUT_MS = 6000;
var CATEGORY_LABELS = CATEGORY_SOURCES.reduce(function (labels, source) {
  labels[source.key] = source.label;
  return labels;
}, {});
```

- [ ] **Step 2: `createReportCard`에 카테고리 배지 추가**

`appendTextElement(heading, "span", "pr-number", "#" + entry.number);` 바로
다음 줄에 추가:

```js
  appendTextElement(
    heading,
    "span",
    "pr-category",
    CATEGORY_LABELS[entry.category] || entry.category
  );
```

- [ ] **Step 3: `initializeArchive()`를 탭+cross-repo fetch 지원 버전으로 교체**

기존 `initializeArchive` 함수 전체를 다음으로 교체:

```js
function initializeArchive() {
  var dataElement = document.getElementById("archive-data");
  var list = document.getElementById("report-list");
  var count = document.getElementById("report-count");
  var search = document.getElementById("archive-search");
  var empty = document.getElementById("empty-state");
  var tabs = document.getElementById("category-tabs");
  if (!dataElement || !list || !count || !search || !empty || !tabs) return;

  var payload;
  try {
    payload = JSON.parse(dataElement.textContent);
  } catch (error) {
    empty.hidden = false;
    empty.textContent = "아카이브 데이터를 읽을 수 없습니다.";
    return;
  }

  var reports = tagCategory(
    Array.isArray(payload.reports) ? payload.reports : [],
    "application"
  );
  var selectedCategory = "all";

  function render(rawQuery) {
    var visible = reports
      .filter(function (entry) {
        return matchesCategory(entry, selectedCategory);
      })
      .filter(function (entry) {
        return matchesArchiveEntry(entry, rawQuery);
      });
    list.replaceChildren();
    visible.forEach(function (entry) {
      list.appendChild(createReportCard(entry));
    });
    count.textContent = String(visible.length);
    empty.hidden = visible.length !== 0;
    empty.textContent = rawQuery
      ? "검색 결과가 없습니다."
      : "아직 등록된 merge 리포트가 없습니다.";
  }

  function loadExternalCategory(source) {
    var controller =
      typeof AbortController !== "undefined" ? new AbortController() : null;
    var timeoutId = controller
      ? setTimeout(function () {
          controller.abort();
        }, FETCH_TIMEOUT_MS)
      : null;
    fetch(source.url, controller ? { signal: controller.signal } : {})
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (externalPayload) {
        if (timeoutId) clearTimeout(timeoutId);
        var entries = Array.isArray(externalPayload.reports)
          ? externalPayload.reports
          : [];
        entries = tagCategory(entries, source.key);
        entries = absolutizeReportUrl(entries, source.base);
        reports = mergeAndSortReports([reports, entries]);
        render(search.value);
      })
      .catch(function (error) {
        if (timeoutId) clearTimeout(timeoutId);
        console.warn(
          "[archive] failed to load category '" + source.key + "':",
          error
        );
      });
  }

  search.addEventListener("input", function () {
    render(search.value);
  });

  var tabButtons = tabs.querySelectorAll("[data-category]");
  tabButtons.forEach(function (button) {
    button.addEventListener("click", function () {
      selectedCategory = button.getAttribute("data-category");
      tabButtons.forEach(function (other) {
        var isSelected = other === button;
        other.setAttribute("aria-selected", String(isSelected));
        other.classList.toggle("is-selected", isSelected);
      });
      render(search.value);
    });
  });

  render("");
  CATEGORY_SOURCES.filter(function (source) {
    return !!source.url;
  }).forEach(loadExternalCategory);
}
```

- [ ] **Step 4: `archive-template.html`에 탭 마크업 추가**

`<label for="archive-search">PR 번호·제목·작성자 검색</label>` 바로 위에 추가:

```html
    <div class="category-tabs" id="category-tabs" role="tablist" aria-label="카테고리 필터">
      <button type="button" class="tab-button is-selected" data-category="all" role="tab" aria-selected="true">전체</button>
      <button type="button" class="tab-button" data-category="application" role="tab" aria-selected="false">Application</button>
      <button type="button" class="tab-button" data-category="airflow" role="tab" aria-selected="false">Airflow</button>
    </div>
```

- [ ] **Step 5: `archive-template.html`에 탭·배지 CSS 추가**

`.archive-count { ... }` 규칙 바로 다음에 추가:

```css
  .category-tabs {
    display: flex;
    gap: 8px;
    margin-bottom: 16px;
    flex-wrap: wrap;
  }
  .tab-button {
    padding: 6px 14px;
    border: 1px solid var(--border);
    border-radius: 999px;
    background: var(--surface);
    color: var(--text);
    font: inherit;
    font-size: .85rem;
    font-weight: 600;
    cursor: pointer;
  }
  .tab-button.is-selected {
    border-color: var(--accent);
    background: var(--accent-soft);
    color: var(--accent);
  }
  .pr-category {
    flex: 0 0 auto;
    padding: 2px 8px;
    border-radius: 999px;
    background: var(--accent-soft);
    color: var(--accent);
    font-size: .72rem;
    font-weight: 700;
  }
```

- [ ] **Step 6: 순수함수 회귀 테스트 재확인**

Run: `cd /home/yjlee/Autoresearch && uv run python -m pytest tests/test_pr_report_archive_search.py tests/test_pr_report_archive_category.py tests/test_pr_report_archive_merge.py -v`
Expected: 전부 PASS — `initializeArchive` 교체가 export된 순수함수 시그니처를
바꾸지 않았는지 재확인.

- [ ] **Step 7: 문법 검증 + 커밋**

```bash
cd /home/yjlee/Autoresearch
node --check .github/pr-report/archive.js && echo "archive.js syntax OK"
python3 -c "
from pathlib import Path
html = Path('.github/pr-report/archive-template.html').read_text()
assert 'category-tabs' in html
assert html.count('data-category') == 3
print('template OK')
"
git add .github/pr-report/archive.js .github/pr-report/archive-template.html
git commit -m "feat: PR 리포트 아카이브에 Airflow 카테고리 탭 추가 (#368)

Autoresearch-airflow의 archive.json을 동일 origin fetch로 가져와
application 데이터와 병합하고, 전체/Application/Airflow 탭으로
필터링한다. build_archive.py/pr-report-archive.yml은 무변경 —
병합은 client-side에서만 수행한다(장애 격리).

Closes #368"
git push -u origin 368-feat-pr-리포트-아카이브에-airflow-저장소-카테고리-탭-추가
```

- [ ] **Step 8: PR 생성**

```bash
gh pr create --repo SKYAHO/Autoresearch \
  --title "feat: PR 리포트 아카이브에 Airflow 카테고리 탭 추가" \
  --body "Closes #368. 설계: docs/specs/2026-07-27-cross-repo-pr-report-archive-tabs.md. Autoresearch-airflow#<N>(아카이브 생성기 이식)와 함께 봐야 완전한 그림이 됩니다."
```

---

### Task 5: End-to-end 배포 검증

**Working directory:** 양쪽 저장소 모두

**Files:** 없음(검증 전용, 코드 변경 없음)

**Interfaces:**
- Consumes: Task 1(airflow PR), Task 4(Autoresearch PR) — 둘 다 머지된 상태 필요.
- Produces: 없음(검증 결과만 보고)

- [ ] **Step 1: 두 PR 머지 확인**

```bash
gh pr view <airflow-PR번호> --repo SKYAHO/Autoresearch-airflow --json state,mergedAt
gh pr view <autoresearch-PR번호> --repo SKYAHO/Autoresearch --json state,mergedAt
```

Expected: 둘 다 `"state": "MERGED"`.

- [ ] **Step 2: airflow 아카이브 워크플로우 최초 실행**

```bash
gh workflow run pr-report-archive.yml --repo SKYAHO/Autoresearch-airflow
```

몇 초 후:

```bash
gh run list --repo SKYAHO/Autoresearch-airflow --workflow pr-report-archive.yml --limit 1
```

Expected: `completed success`.

- [ ] **Step 3: airflow archive.json 응답 확인**

```bash
curl -s https://skyaho.github.io/Autoresearch-airflow/archive.json | python3 -m json.tool | head -20
```

Expected: `schema_version: 1`과 `reports` 배열(최소 1개 이상, PR #153 포함).

- [ ] **Step 4: Autoresearch 아카이브 페이지 수동 확인**

```bash
curl -s https://skyaho.github.io/Autoresearch/ | grep -o "category-tabs"
```

Expected: `category-tabs` 출력(탭 마크업 배포 확인).

브라우저(또는 `curl` + 육안 확인이 어려우면 사용자에게 URL 전달)로
`https://skyaho.github.io/Autoresearch/`을 열어:
1. "전체" 탭에 application + airflow 카드가 섞여 있고 각 카드에
   Application/Airflow 배지가 보이는지
2. "Airflow" 탭 클릭 시 airflow 카드만 남고, 카드의 "리포트 보기"
   링크가 `https://skyaho.github.io/Autoresearch-airflow/pr/<N>/`로
   연결되는지
3. 검색창에 airflow PR 번호를 입력했을 때 탭과 무관하게(또는 "전체"
   탭에서) 정상 필터링되는지

확인한다.

- [ ] **Step 5: 결과 보고**

사용자에게 위 4단계 확인 결과와 실제 페이지 URL을 요약해 보고한다.
