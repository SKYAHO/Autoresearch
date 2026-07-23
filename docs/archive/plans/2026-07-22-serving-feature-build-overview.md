# Serving Feature Build Overview Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 비개발 팀원이 PR #220의 서비스 전반 변화를 스스로 읽고 이해할 수 있는 단일 HTML 보고서를 제공한다.

**Architecture:** `docs/reports/`의 정적 HTML은 CSS와 인라인 SVG만 사용해, 변경 전후와 새 요청 경로를 하나의 스크롤 흐름으로 보여 준다. `docs/README.md`는 이 문서를 서빙 영역의 현재 참고 자료로 연결한다.

**Tech Stack:** HTML5, CSS3, 인라인 SVG, 최소 JavaScript, `html.parser`, Playwright 헤드리스 브라우저.

## Global Constraints

- 외부 CDN·이미지·빌드 단계·운영 자격 증명을 사용하지 않는다.
- 구현 상태와 운영 선행 조건을 같은 의미로 표기하지 않는다.
- `/rerank`은 요청 `video_ids` 순서를 보존하며, 요청마다 Feast 배치 조회는 두 번이다.
- 실제 Redis/GKE smoke는 #210, #218, materialize 준비 후의 운영 게이트다.

---

### Task 1: 비개발 팀원용 단일 HTML 보고서

**Files:**
- Create: `docs/reports/2026-07-22-serving-feature-build-overview.html`

**Interfaces:**
- Consumes: `docs/specs/2026-07-16-reranking-serving-api.md`의 API·피처·운영 계약
- Produces: 브라우저에서 직접 열 수 있는 한국어 설명 보고서

- [ ] **Step 1: 보고서의 검증 기준을 먼저 정한다**

문서는 다음의 식별 가능한 섹션과 상태 문구를 포함해야 한다.

```text
변경 전 · 후
새 요청 흐름
프로젝트 전체에 생긴 효과
구현됨
운영 전제
```

- [ ] **Step 2: 반응형 HTML과 인라인 흐름도를 작성한다**

`main` 안에 다음과 같은 흐름을 구현한다. 각 노드는 짧은 일상어 설명과 기술 이름을
함께 갖는다.

```html
<div class="flow" aria-label="새 요청 흐름">
  <article class="flow-node"><strong>사용자와 영상 ID</strong><span>/rerank 요청</span></article>
  <svg aria-hidden="true" viewBox="0 0 48 20"><path d="M2 10 H40 M34 4 L42 10 L34 16" /></svg>
  <article class="flow-node"><strong>정보를 두 번에 모음</strong><span>Feast 배치 조회</span></article>
</div>
```

상단에는 핵심 메시지와 상태 배지를, 본문에는 변경 전후·요청 흐름·효과·소유 경계·
검증/다음 조건을 이 순서로 둔다. 작은 화면에서는 흐름 노드가 세로로 쌓이도록 media
query를 둔다.

- [ ] **Step 3: 정적 구조 검증을 실행한다**

Run:

```bash
uv run --no-sync python -c "from html.parser import HTMLParser; HTMLParser().feed(open('docs/reports/2026-07-22-serving-feature-build-overview.html', encoding='utf-8').read())"
```

Expected: exit code 0.

### Task 2: 문서 인덱스 연결과 렌더링 검증

**Files:**
- Modify: `docs/README.md`
- Test: `docs/reports/2026-07-22-serving-feature-build-overview.html` (browser rendering)

**Interfaces:**
- Consumes: Task 1의 HTML 경로와 제목
- Produces: 서빙 문서 인덱스에서 접근 가능한, 데스크톱·모바일에서 읽히는 보고서

- [ ] **Step 1: 서빙 인덱스에 보고서를 연결한다**

`docs/README.md`의 `### 🚀 서빙 (Serving)`에 다음 링크를 추가한다.

```markdown
- [시각화 — Serving Feature Build: 무엇이 바뀌었나](reports/2026-07-22-serving-feature-build-overview.html) — 비개발 팀원용 변경 흐름·운영 경계 안내
```

- [ ] **Step 2: 브라우저 렌더링과 모바일 폭을 검증한다**

Playwright로 1440px과 390px 너비에서 HTML을 열고, 페이지 높이·가로 overflow·주요
문구 존재를 확인한다.

```python
assert page.locator("h1").inner_text() == "Serving Feature Build"
assert page.locator("body").evaluate("element => element.scrollWidth <= window.innerWidth")
assert "운영 전제" in page.locator("body").inner_text()
```

- [ ] **Step 3: 최종 문서 변경을 검증하고 커밋한다**

Run:

```bash
git diff --check
git status --short
```

Expected: 공백 오류 없음. HTML, 문서 인덱스, 이 계획의 변경만 추적됨.
