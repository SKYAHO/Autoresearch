# PR 이해 리포트 가독성 개선 (4단계 재구성) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** PR 이해 리포트 페이지를 1단계(파이프라인)→2단계(쉬운 설명)→3단계(코드 기준 설명, 기본 접힘)→4단계(이해도 확인)로 재구성해, 비전문가 팀원이 기술 용어 없이 빠르게 변경을 이해할 수 있게 한다.

**Architecture:** `report.schema.json`에 신규 필드 `plain_points_ko`를 추가하고 `schema_version`을 2로 올린다. `generate_report.py`의 LLM 프롬프트에 이 필드 작성 rubric을 추가한다. `template.html`은 섹션 순서를 재배치하고 3단계를 `<details>`로 기본 접는다. `pr-report.yml`의 sticky 코멘트는 라벨과 미리보기 소스를 갱신한다. 네 파일 모두 관련 커밋(#334 diff 스파이크로 프롬프트 검증 완료)에 의존하는 하나의 계약 변경이므로 순서대로(스키마 → 프롬프트 → 템플릿 → 워크플로우) 진행한다.

**Tech Stack:** 순수 stdlib Python 3(`generate_report.py`), JSON Schema draft 2020-12(`check-jsonschema`), 바닐라 HTML/CSS/JS(`template.html`, 빌드 도구 없음), GitHub Actions YAML.

## Global Constraints

- 에이전트는 `template.html`을 데이터로 채우기만 하고 구조를 발명하지 않는다 — 스키마가 계약 정본이다 (`docs/specs/2026-07-24-pr-comprehension-report.md`).
- `plain_points_ko`는 3~6개, 항목당 120자 이하, 함수/변수/클래스명 금지, 주제 단위 bullet.
- `changes[]`·`pipeline`·`qa_note_ko`의 필드 구조는 이번 변경 범위 밖이다 — 이름·타입을 바꾸지 않는다.
- 커밋 메시지와 PR 설명은 한국어 격식체.
- 이 저장소에 pytest 스위트가 커버하지 않는 영역(`.github/pr-report/*`)이므로, 검증은 `check-jsonschema`·`node --check`·수동 스크립트 실행으로 한다 (아래 각 태스크에 명시).
- 로컬 검증에 필요한 `check-jsonschema`가 없으면 먼저 설치한다: `python3 -m pip install --user check-jsonschema` (설치 후 `python3 -m check_jsonschema --version`으로 확인).
- 모든 태스크가 공유하는 fixture 파일 경로: `/tmp/pr-report-fixture.json` (저장소에 커밋하지 않음 — Task 1 Step 1에서 생성).

---

## Task 1: report.schema.json — plain_points_ko 필드 추가

**Files:**
- Modify: `.github/pr-report/report.schema.json:6-18` (required 배열, schema_version), `.github/pr-report/report.schema.json:32-38` (summary_ko 다음에 새 속성 삽입)

**Interfaces:**
- Produces: `plain_points_ko` — JSON 배열, 문자열 3~6개, 항목당 1~120자. 이후 모든 태스크가 이 필드명·제약을 그대로 사용한다.

- [ ] **Step 1: fixture 작성 + 실패 확인 (RED)**

`/tmp/pr-report-fixture.json`을 아래 내용으로 만든다 (이 fixture는 Task 3·4에서도 재사용한다):

```bash
cat > /tmp/pr-report-fixture.json <<'EOF'
{
  "schema_version": 2,
  "pr": {
    "number": 340,
    "title": "PR 이해 리포트 가독성 개선",
    "author": "bbungjun",
    "issue_refs": [340],
    "generated_at": "2026-07-24T10:00:00+00:00",
    "head_sha": "abc1234567890"
  },
  "summary_ko": [
    "PR 리포트를 4단계로 재구성해 쉬운 설명을 앞에 배치합니다.",
    "3단계 코드 기준 설명은 기본으로 접혀 있습니다.",
    "sticky 코멘트 미리보기도 쉬운 설명 기반으로 바뀝니다."
  ],
  "plain_points_ko": [
    "리포트를 읽는 순서가 쉬운 설명 먼저, 코드 상세는 나중에 보도록 바뀌어요.",
    "코드 상세 설명은 기본으로 접혀 있어서 원할 때만 펼쳐 볼 수 있어요.",
    "PR 코멘트 미리보기도 어려운 용어 대신 쉬운 문장으로 보여줘요."
  ],
  "motivation_ko": "팀원들이 구현하지 않은 모듈의 기술 설명을 읽는 데 시간이 오래 걸린다는 피드백이 있었습니다.",
  "expected_effects_ko": [
    "비전문가 팀원의 PR 이해 시간 단축",
    "코드 상세를 원하는 사람만 펼쳐보는 선택적 노출"
  ],
  "pipeline": {
    "nodes": [
      { "id": "train_eval", "label": "모델 학습·평가 (LightGBM·MLflow)", "lane": "train", "order": 2, "status": "unchanged" },
      { "id": "registry", "label": "Model Registry (MLflow)", "lane": "train", "order": 3, "status": "unchanged" }
    ],
    "edges": [
      { "from": "train_eval", "to": "registry" }
    ],
    "focus": ["train_eval"]
  },
  "changes": [
    {
      "rank": 1,
      "importance": "config",
      "module": ".github/pr-report",
      "symbol": "report.schema.json",
      "file": ".github/pr-report/report.schema.json",
      "explanation_ko": "plain_points_ko 필드를 추가하고 schema_version을 2로 올렸습니다."
    }
  ],
  "qa_note_ko": "코드리뷰 봇이 diff 인라인에 남긴 '이해도 확인:' 질문에 답변한 뒤 스레드를 resolve해 주십시오."
}
EOF
```

지금(스키마 수정 전) 검증을 돌려 실패하는지 확인한다:

```bash
python3 -m check_jsonschema --schemafile .github/pr-report/report.schema.json /tmp/pr-report-fixture.json
```

Expected: FAIL — `schema_version`이 `1`이어야 하는데 `2`, 그리고 `plain_points_ko`는 `additionalProperties: false`에 걸려 허용되지 않은 속성이라는 오류가 출력된다.

- [ ] **Step 2: 스키마 수정 (GREEN)**

`.github/pr-report/report.schema.json:18`을 수정:

```json
    "schema_version": { "const": 2 },
```

`.github/pr-report/report.schema.json:6-15`의 `required` 배열에 `"plain_points_ko"`를 `"summary_ko"` 다음에 추가:

```json
  "required": [
    "schema_version",
    "pr",
    "summary_ko",
    "plain_points_ko",
    "motivation_ko",
    "expected_effects_ko",
    "pipeline",
    "changes",
    "qa_note_ko"
  ],
```

`.github/pr-report/report.schema.json`의 `summary_ko` 속성 정의(원래 32-38행) 바로 다음, `motivation_ko` 속성 정의 바로 앞에 새 속성을 삽입:

```json
    "plain_points_ko": {
      "type": "array",
      "minItems": 3,
      "maxItems": 6,
      "items": { "type": "string", "minLength": 1, "maxLength": 120 },
      "description": "2단계용 쉬운 설명 — 이 모듈을 모르는 팀원 기준 주제별 bullet (함수/변수/클래스명 지양)"
    },
```

- [ ] **Step 3: 재검증**

```bash
python3 -m check_jsonschema --schemafile .github/pr-report/report.schema.json /tmp/pr-report-fixture.json
```

Expected: `ok -- validation done` (exit code 0)

- [ ] **Step 4: 회귀 확인 — 필드 누락 시 여전히 실패하는지**

```bash
python3 -c "
import json
d = json.load(open('/tmp/pr-report-fixture.json'))
del d['plain_points_ko']
json.dump(d, open('/tmp/pr-report-fixture-missing.json', 'w'))
"
python3 -m check_jsonschema --schemafile .github/pr-report/report.schema.json /tmp/pr-report-fixture-missing.json
```

Expected: FAIL — `'plain_points_ko' is a required property`

- [ ] **Step 5: Commit**

```bash
git add .github/pr-report/report.schema.json
git commit -m "feat: report.schema.json에 plain_points_ko 필드 추가 (schema_version 2) (#340)"
```

---

## Task 2: generate_report.py — plain_points_ko 프롬프트 rubric 추가

**Files:**
- Modify: `.github/pr-report/generate_report.py:132-165` (`build_messages` 함수 내부)

**Interfaces:**
- Consumes: Task 1에서 확정한 `plain_points_ko` 필드 제약 (3~6개, 120자 이하)
- Produces: system 프롬프트에 `plain_points_ko` 작성 규칙 섹션. 이후 실제 LLM 호출(Task 2 Step 4)이 이 규칙을 따르는 JSON을 생성한다.

- [ ] **Step 1: 현재 프롬프트에 rubric이 없는지 확인 (RED)**

```bash
cd /mnt/c/Autoresarch
PR_NUMBER=334 python3 .github/pr-report/generate_report.py --dry-run 2>/dev/null | grep -c "plain_points_ko 작성 규칙"
```

Expected: `0`

- [ ] **Step 2: rubric 섹션 추가**

`.github/pr-report/generate_report.py`의 `build_messages()` 함수에서, 기존 "changes 중요도 rubric" 문단(163행 "...롤백 주의점 등을 적습니다." 로 끝나는 줄) 바로 다음, "summary_ko는 정확히 3줄..." 문단(165행) 바로 앞에 새 섹션을 삽입한다:

```python
## plain_points_ko 작성 규칙
- 이 PR을 구현하지 않은 팀원이 읽습니다. 함수명·변수명·클래스명 대신 "무엇이 어떻게 달라지는지"를 동작·데이터·사용자 영향 중심으로 씁니다.
- 3~6개, 각 항목 1문장(120자 이하). 주제 단위로 묶습니다(예: "데이터가 이렇게 바뀌어요", "학습 방식이 이렇게 바뀌어요").
- 전문용어를 쓸 수밖에 없으면 괄호로 풀어씁니다.
```

같은 함수의 마지막 문단(원래 165행)에서 `plain_points_ko`를 언급하고 "이 넷"을 "이 다섯"으로 바꾼다. 수정 후 전체 문단:

```python
summary_ko는 정확히 3줄, 각 줄 120자 이하입니다. plain_points_ko는 위 작성 규칙에 따라 3~6개 항목을 반드시 채웁니다. motivation_ko에는 이 변경이 왜 필요했는지(as-is의 문제·배경)를 1~3문장으로 반드시 채웁니다. expected_effects_ko에는 기대효과를 1~5개 항목으로 반드시 채웁니다. qa_note_ko에는 "코드리뷰 봇이 diff 인라인에 남긴 '이해도 확인:' 질문에 답변한 뒤 스레드를 resolve해 주십시오"를 이 PR의 핵심 로직 주제와 함께 안내합니다. 이 다섯은 모두 스키마 필수 필드입니다."""
```

- [ ] **Step 3: rubric 삽입 확인 (GREEN)**

```bash
PR_NUMBER=334 python3 .github/pr-report/generate_report.py --dry-run 2>/dev/null | grep -c "plain_points_ko 작성 규칙"
```

Expected: `1`

- [ ] **Step 4: 실제 OpenRouter 호출로 통합 검증**

로컬에 `OPENROUTER_API_KEY`가 설정돼 있어야 한다(`echo $OPENROUTER_API_KEY`로 확인). 실제 PR에 대해 스크립트를 끝까지 돌려, Task 1에서 바뀐 스키마까지 통과하는 `report.json`이 나오는지 확인한다 — 이 호출은 팀 OpenRouter 크레딧을 소량 소비한다.

```bash
cd /mnt/c/Autoresarch
GH_TOKEN=$(gh auth token) PR_NUMBER=334 python3 .github/pr-report/generate_report.py
python3 -m check_jsonschema --schemafile .github/pr-report/report.schema.json report.json
python3 -c "import json; print(json.load(open('report.json'))['plain_points_ko'])"
rm report.json  # 저장소에 커밋되지 않도록 생성물 삭제
```

Expected: `check-jsonschema` 통과(`ok -- validation done`), `plain_points_ko`가 3~6개의 한국어 문장 리스트로 출력됨.

- [ ] **Step 5: Commit**

```bash
git add .github/pr-report/generate_report.py
git commit -m "feat: plain_points_ko 프롬프트 rubric 추가 (#340)"
```

---

## Task 3: template.html — 4단계 페이지 재구성

**Files:**
- Modify: `.github/pr-report/template.html:98-99` (CSS, `#why .effects` 규칙 교체), `.github/pr-report/template.html:170-181` (HTML, `#why`→`#easy` 섹션 + `#changes` 접힘), `.github/pr-report/template.html:183-186` (HTML, `#qa` 라벨), `.github/pr-report/template.html:344-351` (JS, easy 섹션 렌더링), `.github/pr-report/template.html` changes 렌더링 루프 끝 (JS, summary 카운트)

**Interfaces:**
- Consumes: `r.plain_points_ko`(Task 1·2), `r.changes`(기존, 개수만 사용)
- Produces: DOM id `plain-points-list`, `changes-details`, `changes-summary` — 이후 태스크는 이 id들을 참조하지 않으므로 순수 최종 산출물.

- [ ] **Step 1: CSS 교체**

`.github/pr-report/template.html:98-99`의 기존 규칙:

```css
  /* 왜/기대효과 */
  #why .effects { margin: 10px 0 0; padding-left: 22px; }
```

을 아래로 교체:

```css
  /* 2단계: 쉬운 설명 · 왜 · 기대효과 */
  #easy .easy-block { margin-bottom: 14px; }
  #easy .easy-block:last-child { margin-bottom: 0; }
  #easy .easy-block .tag {
    display: block; font-size: .72rem; font-weight: 600; color: var(--muted); margin-bottom: 6px;
  }
  #easy .easy-block ul { margin: 0; padding-left: 22px; }
  #easy .easy-block li { margin: 4px 0; }
  /* 3단계: 기본 접힘 */
  #changes-details > summary {
    cursor: pointer; font-weight: 600; color: var(--accent); padding: 4px 0; user-select: none;
  }
  #changes-details[open] > summary { margin-bottom: 12px; }
```

- [ ] **Step 2: `#why` 섹션을 `#easy`로 교체**

`.github/pr-report/template.html:170-176`의:

```html
  <section id="why">
    <h2>왜 이 작업이 필요했나 · 기대효과</h2>
    <div class="card">
      <p id="motivation" style="margin:0"></p>
      <ul class="effects" id="effects-list"></ul>
    </div>
  </section>
```

을 아래로 교체:

```html
  <section id="easy">
    <h2><span class="step">2단계</span>쉬운 설명</h2>
    <div class="card">
      <div class="easy-block">
        <span class="tag">무슨 일이 있었나</span>
        <ul id="plain-points-list"></ul>
      </div>
      <div class="easy-block">
        <span class="tag">왜 필요했나</span>
        <p id="motivation" style="margin:0"></p>
      </div>
      <div class="easy-block">
        <span class="tag">기대효과</span>
        <ul class="effects" id="effects-list"></ul>
      </div>
    </div>
  </section>
```

- [ ] **Step 3: `#changes` 섹션을 3단계로 재라벨 + 기본 접힘**

`.github/pr-report/template.html:178-181`의:

```html
  <section id="changes">
    <h2><span class="step">2단계</span>중요도순 변경 사항</h2>
    <div id="changes-list"></div>
  </section>
```

을 아래로 교체:

```html
  <section id="changes">
    <h2><span class="step">3단계</span>코드 기준 설명</h2>
    <details id="changes-details">
      <summary id="changes-summary">코드 기준 상세 설명 펼치기</summary>
      <div id="changes-list"></div>
    </details>
  </section>
```

- [ ] **Step 4: `#qa` 섹션 라벨을 4단계로**

`.github/pr-report/template.html:183-186`의:

```html
  <section id="qa">
    <h2><span class="step">3단계</span>이해도 확인</h2>
    <div class="card"><p id="qa-note" style="margin:0"></p></div>
  </section>
```

을 아래로 교체 (라벨 숫자만 변경):

```html
  <section id="qa">
    <h2><span class="step">4단계</span>이해도 확인</h2>
    <div class="card"><p id="qa-note" style="margin:0"></p></div>
  </section>
```

- [ ] **Step 5: JS — plain_points_ko 렌더링 추가**

`.github/pr-report/template.html:344-351`의:

```js
  // ---------- why ----------
  document.getElementById("motivation").textContent = r.motivation_ko;
  var ul = document.getElementById("effects-list");
  r.expected_effects_ko.forEach(function (t) {
    var li = document.createElement("li");
    li.textContent = t;
    ul.appendChild(li);
  });
```

을 아래로 교체:

```js
  // ---------- easy (plain points / why / effects) ----------
  var ppList = document.getElementById("plain-points-list");
  r.plain_points_ko.forEach(function (t) {
    var li = document.createElement("li");
    li.textContent = t;
    ppList.appendChild(li);
  });
  document.getElementById("motivation").textContent = r.motivation_ko;
  var ul = document.getElementById("effects-list");
  r.expected_effects_ko.forEach(function (t) {
    var li = document.createElement("li");
    li.textContent = t;
    ul.appendChild(li);
  });
```

- [ ] **Step 6: JS — changes 개수를 접힘 summary에 표시**

changes 카드 렌더링 루프(`r.changes.slice().sort(...).forEach(function (c) { ... list.appendChild(card); });`) 바로 다음 줄에 추가:

```js
  document.getElementById("changes-summary").textContent =
    "코드 기준 상세 설명 펼치기 (" + r.changes.length + "건)";
```

- [ ] **Step 7: 정적 검증 — inject.py 실행 + JS 문법 확인**

```bash
cd /mnt/c/Autoresarch
python3 .github/pr-report/inject.py .github/pr-report/template.html /tmp/pr-report-fixture.json > /tmp/pr-report-out.html
grep -c 'id="plain-points-list"' /tmp/pr-report-out.html
grep -c 'id="changes-details"' /tmp/pr-report-out.html
grep -c '2단계</span>쉬운 설명' /tmp/pr-report-out.html
grep -c '4단계</span>이해도 확인' /tmp/pr-report-out.html
python3 -c "
import re
html = open('/tmp/pr-report-out.html', encoding='utf-8').read()
m = re.search(r'<script>\n\"use strict\";(.*?)</script>', html, re.DOTALL)
open('/tmp/pr-report-script.js', 'w', encoding='utf-8').write('\"use strict\";' + m.group(1))
"
node --check /tmp/pr-report-script.js
```

Expected: 각 `grep -c` 결과 `1` 이상, `node --check` 무출력(exit 0, 문법 오류 없음).

- [ ] **Step 8: 수동 시각 확인 (자동화 불가 — 반드시 수행)**

`/tmp/pr-report-out.html`을 브라우저로 열어 다음을 눈으로 확인한다 (이 저장소에는 헤드리스 브라우저/스크린샷 도구가 없어 위 단계로 대체할 수 없다):

- 2단계 카드 안에 "무슨 일이 있었나"/"왜 필요했나"/"기대효과" 세 블록이 시각적으로 구분되는지
- 3단계가 기본 접혀 있고, 펼치면 기존 changes 카드(배지·diff 토글 포함)가 그대로 나오는지
- 4단계 제목이 "이해도 확인"으로 남아있는지
- OS 다크모드 전환 시 색상이 깨지지 않는지

- [ ] **Step 9: Commit**

```bash
git add .github/pr-report/template.html
git commit -m "feat: 리포트 페이지를 4단계(쉬운 설명 우선)로 재구성 (#340)"
```

---

## Task 4: pr-report.yml — sticky 코멘트 라벨·미리보기 갱신

**Files:**
- Modify: `.github/workflows/pr-report.yml:154-175` (`Upsert sticky comment` 스텝의 인라인 Python)

**Interfaces:**
- Consumes: `r["plain_points_ko"]` (Task 1)
- Produces: 없음 (최종 산출물, sticky 코멘트 본문 문자열)

- [ ] **Step 1: 현재 스니펫을 파일로 추출해 기존 동작 확인 (RED)**

```bash
cd /mnt/c/Autoresarch
python3 - <<'PYEOF' > /tmp/extract_snippet.py
import re
yml = open(".github/workflows/pr-report.yml", encoding="utf-8").read()
m = re.search(r"python - <<'PYEOF'\n(.*?)\n          PYEOF", yml, re.DOTALL)
body = "\n".join(line[10:] if line.startswith(" " * 10) else line for line in m.group(1).splitlines())
print(body)
PYEOF
cd /tmp && cp /tmp/pr-report-fixture.json report.json
PAGES_URL="https://skyaho.github.io/Autoresearch/pr/340/" python3 /tmp/extract_snippet.py > /tmp/sticky_body_before.txt
grep -c "중요 변경 top 3" /tmp/sticky_body_before.txt
grep -c "쉬운 설명" /tmp/sticky_body_before.txt
```

Expected: 첫 번째 `grep -c` → `1` (옛 라벨이 아직 있음), 두 번째 `grep -c` → `0` (새 라벨 아직 없음)

- [ ] **Step 2: 스니펫 교체**

`.github/workflows/pr-report.yml:154-175`의 (`BODY="$(python - <<'PYEOF' ... PYEOF)"` 블록 내부):

```python
          lines = ["<!-- pr-comprehension-report -->", "## PR 이해 리포트", ""]
          lines += [f"{i}. {s}" for i, s in enumerate(r["summary_ko"], 1)]
          lines += ["", "**중요 변경 top 3**", ""]
          for c in sorted(r["changes"], key=lambda c: c["rank"])[:3]:
              first = c["explanation_ko"].splitlines()[0]
              lines.append(f"- `{c['importance']}` **{c['module']} :: {c['symbol']}** — {first}")
          lines += [
              "",
              f"**[1·2단계 — 파이프라인 시각화·중요도순 전체 diff 보기]({url})** (배포 반영에 1~2분 걸릴 수 있습니다)",
              "",
              "**3단계 — 이해도 확인:** 코드리뷰 봇이 diff 인라인에 남긴 '이해도 확인:' 질문에 답변한 뒤 스레드를 resolve해 주십시오.",
              "",
              "---",
              "_이 코멘트는 pr-report 워크플로우가 자동 생성했습니다. 코드가 갱신되면 PR에 `/claude-report` 코멘트로 재생성할 수 있습니다._",
          ]
```

을 아래로 교체:

```python
          lines = ["<!-- pr-comprehension-report -->", "## PR 이해 리포트", ""]
          lines += [f"{i}. {s}" for i, s in enumerate(r["summary_ko"], 1)]
          lines += ["", "**쉬운 설명**", ""]
          lines += [f"- {p}" for p in r["plain_points_ko"][:3]]
          lines += [
              "",
              f"**[1·2단계 — 파이프라인 시각화·쉬운 설명 보기]({url})** (배포 반영에 1~2분 걸릴 수 있습니다)",
              "",
              "**4단계 — 이해도 확인:** 코드리뷰 봇이 diff 인라인에 남긴 '이해도 확인:' 질문에 답변한 뒤 스레드를 resolve해 주십시오.",
              "",
              "---",
              "_이 코멘트는 pr-report 워크플로우가 자동 생성했습니다. 코드가 갱신되면 PR에 `/claude-report` 코멘트로 재생성할 수 있습니다._",
          ]
```

- [ ] **Step 3: YAML 파싱 + 재검증 (GREEN)**

```bash
cd /mnt/c/Autoresarch
python3 -c "import yaml; yaml.safe_load(open('.github/workflows/pr-report.yml')); print('yaml ok')"
python3 - <<'PYEOF' > /tmp/extract_snippet.py
import re
yml = open(".github/workflows/pr-report.yml", encoding="utf-8").read()
m = re.search(r"python - <<'PYEOF'\n(.*?)\n          PYEOF", yml, re.DOTALL)
body = "\n".join(line[10:] if line.startswith(" " * 10) else line for line in m.group(1).splitlines())
print(body)
PYEOF
cd /tmp
PAGES_URL="https://skyaho.github.io/Autoresearch/pr/340/" python3 /tmp/extract_snippet.py > /tmp/sticky_body_after.txt
grep -c "중요 변경 top 3" /tmp/sticky_body_after.txt
grep -c "쉬운 설명" /tmp/sticky_body_after.txt
grep -c "4단계 — 이해도 확인" /tmp/sticky_body_after.txt
cat /tmp/sticky_body_after.txt
```

Expected: `yaml ok` 출력, 첫 `grep -c` → `0`, 두 번째·세 번째 `grep -c` → `1`. 마지막 `cat`에서 fixture의 `plain_points_ko` 3개 항목이 `- ` bullet로 출력되는지 육안 확인.

- [ ] **Step 4: 정리 + Commit**

```bash
cd /mnt/c/Autoresarch
rm -f /tmp/report.json /tmp/extract_snippet.py /tmp/sticky_body_before.txt /tmp/sticky_body_after.txt /tmp/pr-report-script.js /tmp/pr-report-out.html /tmp/pr-report-fixture.json /tmp/pr-report-fixture-missing.json
git add .github/workflows/pr-report.yml
git commit -m "feat: sticky 코멘트 라벨·미리보기를 4단계·plain_points_ko 기준으로 갱신 (#340)"
```

---

## 최종 확인

- [ ] `uv run --no-sync ruff check .github/pr-report` — Expected: `All checks passed!`
- [ ] `git log --oneline -5` — Task 1~4 커밋 4개가 순서대로 보이는지 확인
- [ ] `git diff docs/specs/2026-07-24-pr-comprehension-report.md main -- docs/specs/2026-07-24-pr-comprehension-report.md` 등으로 spec과 실제 구현이 어긋나지 않는지 최종 대조 (spec의 "가독성 개선" 절 내용과 Task 1~4 결과 일치 확인)
- [ ] PR 생성 시 이슈 #340을 `Closes #340`으로 연결
