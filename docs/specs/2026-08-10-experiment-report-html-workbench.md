# 실험 리포트를 워크벤치에서 HTML 페이지로 렌더한다

> 2026-08-10 | 상태: 구현 완료 (#647) | #647
>
> `docs/specs/2026-08-09-agent-authored-experiment-report.md`가 정의한 산출물 계약
> (`report.md`)을 **읽는 쪽**으로 잇는다. 그 계약의 결정은 그대로 유효하며 여기서
> 뒤집지 않는다. 같은 문서의 Stage 2(컨테이너 8→4)와는 독립이고 컨테이너 구성을
> 건드리지 않는다.

## 요약

실험의 최종 산출물인 `report.md`를 Streamlit 워크벤치 결과 탭까지 가져와, **우리가
소유하는 고정 HTML 템플릿**으로 렌더한다. 본문은 executor가 완주 보고에 함께 실어
`experiments.report_markdown`에 적재하고, UI는 별도 조회 endpoint로 받아 markdown을
HTML로 변환해 iframe에 넣는다.

관통하는 원칙은 하나다 — **리포트는 지표에 종속된다.** 리포트 때문에 지표가 사라지는
경로를 executor·API·DB·UI 어디에도 남기지 않는다. 이 문서의 결정 2·3·7이 각 계층에서
같은 성질을 지킨다.

## 배경 — 왜 바꾸는가

`report.md`는 #640·#643으로 실제로 쓰이기 시작했고 GCS에 게시된다. 그런데 **워크벤치
어디에도 보이지 않는다.**

결과 탭(`agent_orchestration/ui/views.py:343` `_render_metrics`)은 `metric_summary`
dict를 순회하며 `st.metric` 또는 `st.code(json)`으로 찍는 것이 전부다. `results_uri`도
문자열 하나로 찍힐 뿐 링크가 아니며, UI가 GCS를 읽는 경로는 없다.

"에이전트가 실험을 수행하고 결과를 서술한다"가 이 프로젝트의 핵심인데, 그 서술이
GCS에만 쌓이고 화면에는 숫자 dump만 남는다. 데모에서 드러나야 할 것이 드러나지 않는다.

## 결정 1 — 보고는 기존 endpoint에 필드를 더하고, 조회는 별도 endpoint를 낸다

`ExperimentResponse`는 5초 polling으로 반복 조회되고 목록 화면에도 실린다. 수십 KB
텍스트를 여기 실으면 목록까지 느려진다 — `measurement.build_metric_snapshot`이 전문이
아니라 요약을 싣는 것과 같은 논리다. 그래서 조회는 `GET /experiments/{id}/report`로
분리한다.

반대로 **보고는 호출을 늘리지 않는다.** `ExecutorResultReportRequest`에 nullable
`report_markdown`을 더한다. 지표와 리포트가 한 번의 보고로 함께 오므로, "리포트 실패가
지표 게시를 막지 않는다"는 현행 성질(`phase2._write_report_if_enabled`)이 유지된다.

### 컬럼은 deferred로 둔다

`experiments.report_markdown`(Text, nullable)을 추가하되 모델에서
`mapped_column(Text, nullable=True, deferred=True)`로 선언한다.

**근거:** 응답 스키마에서 빼는 것만으로는 비용이 사라지지 않는다.
`find_experiments`는 `select(Experiment)`로 **전체 컬럼**을 읽는다
(`agent_orchestration/app/experiments/repository.py:48`). 목록 상한이 100행이므로 평범한
컬럼으로 두면 한 번의 목록 조회가 최대 100 × 64KB를 DB에서 끌어온다. 응답에서 감추는
것과 읽지 않는 것은 다르다.

deferred는 이 저장소의 첫 사용례다. 이유를 모델 docstring에 남겨, 나중에 "왜 이것만
다른가"를 코드 밖에서 찾지 않게 한다. 조회 endpoint의 service는 `undefer`로 이 컬럼만
명시 로드한다.

### 응답 형식

`ExperimentReportResponse { experiment_id, report_markdown: str | None }`.

- 실험이 없으면 **404**
- 실험은 있고 리포트가 없으면 **200 + `null`**

리포트 없음을 404로 만들면 UI가 "실험이 사라졌다"와 "아직 리포트가 없다"를 구별할 수
없다. 후자는 오류가 아니라 정상 상태다 — 완주 전 실험, 리포트를 끄고 돌린 배포,
Codex가 실패한 실행이 모두 여기 해당한다. 인증은 기존 `X-Orch-Token`을 그대로 쓴다.

## 결정 2 — 리포트 쓰기를 지표 커밋과 다른 트랜잭션에 둔다

`record_experiment_result`는 지표와 상태 전이를 먼저 커밋하고, **그 다음에** 별도
트랜잭션으로 `report_markdown`을 쓴다.

```
with session.begin():            # (1) 지표 + 전이 — 현행 코드 그대로
    ...
if request.report_markdown is not None:
    _store_report_markdown(...)  # (2) 별도 트랜잭션
return experiment
```

**근거:** 같은 트랜잭션에 두면 리포트 쓰기 실패가 지표까지 롤백시킨다. 이것은 가상의
위험이 아니라 두 개의 구체적인 경로다.

- **NUL 바이트.** PostgreSQL은 text 값에 `U+0000`을 저장하지 못한다. `report.md`는
  Codex가 쓰고 `read_text(errors="replace")`로 읽히는데, `errors="replace"`는 잘못된
  UTF-8만 바꿀 뿐 **정상 디코드되는 0x00은 `\x00`으로 통과시킨다.** flush에서 psycopg가
  던지고 트랜잭션 전체가 날아간다.
- **배포 순서 어긋남.** migration `0006` 이전에 코드가 뜨면 deferred 컬럼이라 `SELECT`는
  통과하고 `UPDATE`에서 `UndefinedColumn`이 난다. 하필 deferred라서 커밋 직전에 터진다.

`_store_report_markdown`은 전체를 `except Exception`으로 감싸 로그만 남기고 삼킨다 —
리포트 쓰기 실패가 **이미 커밋된 지표 보고를 200에서 500으로 바꾸면 안 된다.**
`with session.begin()`의 `__exit__`가 이미 rollback하므로 명시적 rollback은 넣지 않는다
(`service.py:456`이 같은 근거로 두고 있는 주석과 같다). 그 주석이 기록한 대로 커밋
이후의 SELECT는 새 implicit transaction을 열므로, `_store_report_markdown`은 커밋 직후
**첫 DB 접근으로** `with session.begin()`을 연다.

SAVEPOINT는 쓰지 않는다. 저장소에 선례가 없고, 별도 트랜잭션이 배포 순서 어긋남까지
함께 막는다.

### write-once

이미 값이 있으면 덮어쓰지 않는다. 지표는 다르면 409지만 리포트는 그렇게 하지 않는다 —
재시도가 리포트 때문에 실패하면 지표 보고까지 잃고, 그건 이 경로가 지키는 성질과
정반대다.

다만 조용히 버리지도 않는다. 기존 값과 **다른** 본문이 재시도로 오면 WARNING 한 줄을
남기고 응답은 200으로 둔다.

```
report_markdown ignored: already set, mismatch on retry experiment_id=<id>
```

로그에 본문은 싣지 않는다 — LLM 산출물이고 최대 64KB다. `experiments/service.py`에는
아직 logger가 없어 `app/main.py`·`app/llm.py`와 같은 이름으로 하나 만든다.

## 결정 3 — `report_markdown` 때문에 요청을 거절하는 경로를 남기지 않는다

정규화(NUL 제거, 바이트 상한 절단)를 **Pydantic validator가 아니라 service에서** 한다.
schema에는 망가진 client를 막는 성긴 `max_length=262144` 하나만 둔다. 이것은 **문자 수**
상한이라 바이트 상한과 다르며, 실제 상한을 정하는 것이 아니라 요청 본문이 무한정 커지는
것만 막는다. DB에 들어갈 값의 크기는 service의 절단이 정한다.

**근거:** 거절 경로를 하나라도 남기면 "리포트 내용이 지표 보고를 죽인다"는 결합이 HTTP
계층에서 되살아난다. 결정 2가 트랜잭션에서 끊어낸 것을 422가 다시 이어붙이는 셈이다.
`metric_snapshot` validator는 지금처럼 거절한다 — 그쪽은 fail-closed가 맞다.

정규화는 순수 함수로 두고, **바꾼 게 있으면 WARNING을 남긴다.** 조용한 정규화가 아니라
기록되는 정규화다.

### 잘림은 executor가 먼저 한다

`executor/report.py`에 순수 함수 `truncate_report_markdown(text) -> str`을 둔다. 고정
문구의 바이트를 예산에서 먼저 빼고, 남은 예산만큼 UTF-8 바이트로 자른 뒤
`errors="ignore"` 디코드로 멀티바이트 경계를 맞추고, 잘렸을 때만 문구를 끝에 붙인다 —
같은 모듈의 `_DIFF_TRUNCATION_NOTE` 선례를 따른다.

executor는 HTTP 페이로드를 묶고 API는 DB에 들어갈 값을 묶는다. 둘 다 필요하고, 둘 다
거절이 아니라 절단이다. `executor`는 `app` 패키지를 import하지 않으므로 상한 상수는
양쪽에 두되 **두 값이 같은지 확인하는 테스트**를 단다. `contracts.py`는 LLM 결과 계약
전용 모듈이라 여기에 끌어들여 책임을 넓히지 않는다.

`MAX_REPORT_MARKDOWN_BYTES = 65536`.

### executor 배선 — 본문을 꺼낼 통로가 지금 없다

`_measure_and_publish_if_enabled`는 내부에서 `report_path`를 만들어 **GCS 게시에만 쓰고
버린다**(`agent_orchestration/executor/phase2.py:594-608`). 반환값은 snapshot 하나뿐이라,
본문이 `report_result` 호출부(`phase2.py:645`)까지 닿는 경로가 없다.

이 함수의 반환을 snapshot과 리포트 본문 두 값으로 넓힌다. 리포트가 없으면 본문은
`None`이고, `api_client.report_result`는 `None`이면 payload에 키를 싣지 않는다
(`ui/client.py`의 `patch_status`가 `metric_snapshot`에 쓰는 방식과 같다).

본문을 읽는 것 자체가 실패해도(파일이 사라짐, 권한, 디코드) **위로 올리지 않고** `None`으로
보고를 계속한다. `report_result` 호출부의 주석이 "채점했으면 반드시 보고한다"를 명시하고
있고, 그 보고를 리포트 읽기 실패로 잃으면 `metric_summary=null`이 다시 나온다.

## 결정 4 — 변환은 UI에서 하고, HTML 껍데기는 우리가 소유한다

executor와 GCS는 지금처럼 md만 다룬다.

**근거:** 템플릿을 고치면 과거 실험도 전부 새 디자인으로 보이고, 실험을 다시 돌릴 필요가
없다. GCS는 write-once라 executor에서 변환하면 소급이 불가능하다.

**에이전트는 산문만 쓴다.** 리포트를 쓰는 Codex의 입력에는 외부 사용자가 작성한 GitHub
이슈 본문 원문이 들어간다(`executor/prompt.py:build_report_prompt`). 에이전트가 쓴
HTML을 그대로 렌더하면 현재 `views.py`가 지키고 있는 escape 경계를 처음으로 넘게 된다.

`agent_orchestration/ui/report.py`를 신설한다. Streamlit을 import하지 않는 순수 함수
모듈이다.

- `render_report_html(markdown_text) -> str` —
  `MarkdownIt("commonmark", {"html": False}).enable("table")`

> `[정정 — #647, 2026-08-10]` 처음에는 `.enable("table")` 없이 `commonmark` 프리셋만
> 적었다. 그 프리셋에는 표 확장이 없어, 리포트의 baseline·candidate 비교표가
> `| 지표 | delta |` 파이프 문자 그대로 한 문단에 찍힌다. 리포트에서 표는 장식이
> 아니라 **주 지표가 놓이는 자리**이므로(`prompt.REPORT_SECTIONS`의 `## 주 지표`)
> 그대로 두면 이 이슈가 하려던 일이 화면에서 성립하지 않는다.
>
> `gfm-like` 프리셋은 쓰지 않는다 — linkify가 켜져 있어 `linkify-it-py` 없이는 렌더가
> 예외로 죽고, 그 의존성을 새로 들일 이유가 없다. 표 규칙은 raw HTML escape나
> `validateLink`를 건드리지 않으므로 결정 5의 방어는 그대로다.
- `build_report_document(body_html) -> str` — 고정 CSS를 인라인한 `<!doctype html>` 조립

**템플릿에 스크립트를 한 줄도 넣지 않는다.** 결정 5가 그 이유다.

### 실측으로 확인한 방어 (2026-08-10)

`markdown-it-py` 4.2.0에 `html=False`로 확인했다.

| 입력 | 출력 |
| --- | --- |
| `<script>alert(1)</script>` (인라인) | `&lt;script&gt;alert(1)&lt;/script&gt;` |
| `<div onclick="x">` (블록) | `&lt;div onclick=&quot;x&quot;&gt;` |
| `[js](javascript:alert(1))` | 링크가 아닌 리터럴 텍스트 |
| `[data](data:text/html,x)` | 링크가 아닌 리터럴 텍스트 |
| `[ok](https://e.com)` | `<a href="https://e.com">` |
| `![img](data:image/png;base64,...)` | `<img src="data:image/png;...">` |

`data:image/*`가 통과하는 것은 markdown-it 기본 `validateLink`의 의도된 허용이며 그대로
둔다. 이미지 데이터 URI는 스크립트를 실행하지 않는다.

## 결정 5 — iframe은 격리 경계가 아니다. 방어는 escape 하나다

`st.iframe`에 HTML 문자열을 넘기면 srcdoc으로 들어가고
(`streamlit/elements/iframe.py:378`), Streamlit은 `scrolling`을 항상 켠다. 고정 `height`를
주면 **고정 높이 + 내부 스크롤**이 우리 코드 없이 나온다. `postMessage` 자동 리사이즈를
직접 넣지 않는다 — 실패하면 리포트가 안 보이는 실패 모드만 늘어난다.

**그러나 이것을 보안 경계로 세지 않는다.** Streamlit 1.60.0의 iframe sandbox는 고정
목록이고 `allow-same-origin`과 `allow-scripts`를 **둘 다** 포함한다. srcdoc이므로 부모와
같은 origin이다. Streamlit 자신의 docstring이 "Never pass untrusted HTML ... or LLM
output"이라고 경고한다.

따라서 유일한 방어는 **결정 4의 escape**다. iframe은 레이아웃 수단이지 방어층이 아니다.
템플릿에 스크립트를 넣지 않는 이유가 이것이다 — 격리가 없는 곳에 우리 손으로 스크립트
실행 표면을 만들 이유가 없다.

`st.html`은 쓰지 않는다. 부모 문서에 직접 주입되어 iframe조차 없다.
`st.components.v1.html`도 쓰지 않는다. 동작은 같지만 1.60에서 deprecated이며 "미래
버전에서 제거" 예고된 API에 새 기능을 얹는 셈이 된다.

`orchestration-ui` 그룹에 `markdown-it-py`를 더하고 `streamlit` 하한을 `>=1.60`으로
올린다.

## 결정 6 — 지표는 Streamlit이 그리고, iframe에는 본문만 넣는다

결과 탭은 위에서부터 이렇게 구성한다.

1. `split_matches`가 false면 `st.warning` — "이 delta는 변경의 효과로 읽을 수 없다".
   지표보다 **위**다.
2. 지표 카드 — 세 지표(ROC-AUC / LogLoss / Brier)의 baseline·candidate 평균과 paired
   `mean`. `standard_error`가 `None`이면 표기를 생략한다(seed가 하나면 계산되지 않는다).
3. 리포트 본문 — `st.iframe(html, height=<고정 px>)`
4. 결손·오류 표시 — 판정표는 결정 7의 「상태」에 있다

**근거:** 지표·경고까지 템플릿 안에 넣으면 우리가 직접 HTML·CSS로 카드를 그려야 하고,
고정 높이 안에 경고까지 들어가 긴 리포트에서는 경고가 스크롤 밖으로 밀려난다. 지표를
Streamlit 네이티브로 두면 사용자 테마·반응형을 그대로 받고, "두 결손이 독립"이라는
성질이 구조상 자연히 성립한다.

**에이전트 텍스트를 파싱하지 않는다.** 숫자는 전부 `metric_summary`에서 온다.

### seed별 delta는 그리지 않는다

`build_metric_snapshot`은 `paired[name] = {"mean", "standard_error"}`만 싣는다
(`agent_orchestration/executor/measurement.py:297`). `per_seed` delta는 전문
(`experiment-metrics-v1`)에만 있고 그것은 GCS에 있다.

화면 사정으로 스냅샷 계약을 넓히지 않는다 — 요약이 화면 사정으로 바뀌어도 전문의
형태는 그대로여야 한다는 것이 그 계약을 따로 둔 이유다(`measurement.py` 모듈 docstring).
UI가 GCS를 읽는 경로도 새로 만들지 않는다.

### 오른쪽 "실행 요약" 패널은 건드리지 않는다

`_render_metrics`는 결과 탭(`views.py:319`)과 inspector 패널(`views.py:255`) **두 곳**에서
쓰인다. 이 이슈는 결과 탭만 교체하고 `_render_metrics` 자체는 그대로 둔다. 좁은 패널의
요약 dump는 이 이슈가 해결하려는 문제가 아니다.

## 결정 7 — UI에서도 리포트 실패가 워크벤치를 죽이지 않는다

### 상태

`ui/state.py`에 세 필드를 더하고 `select_experiment`의 초기화 목록에 넣는다 — 실험을
바꿨는데 이전 리포트가 남으면 안 된다.

- `report_markdown: str | None`
- `report_error: str | None`
- `report_loaded_for: str | None`

`report_markdown = None`은 **두 가지 뜻으로 겹친다** — "아직 안 받음"과 "받았는데 리포트가
없음". `report_loaded_for`가 이를 가른다.

| 상태 | 화면 |
| --- | --- |
| `report_error is not None` | `st.warning` (fetch 실패) |
| `report_loaded_for != selected_id` | 아무것도 안 그림 |
| `report_loaded_for == selected_id`, 본문 `None` | caption "아직 리포트가 없습니다" |
| 본문 있음 | `st.iframe` |

> `[정정 — #647, 2026-08-10]` 위 표의 둘째 행은 구현(`views.py`의 `_render_report`)과
> 어긋난다. 구현은 `report_loaded_for != selected_id`일 때 아무것도 안 그리지 않고
> caption "리포트를 불러오는 중입니다."를 낸다. **구현이 낫다** — 아무것도 안 그리면
> polling 중인지 정지했는지 화면에서 구별할 수 없다. 표를 구현에 맞춘다.
>
> | 상태 | 화면 |
> | --- | --- |
> | `report_error is not None` | `st.warning` (fetch 실패) |
> | `report_loaded_for != selected_id` | caption "리포트를 불러오는 중입니다." |
> | `report_loaded_for == selected_id`, 본문 `None` | caption "아직 리포트가 없습니다" |
> | 본문 있음 | `st.iframe` |
>
> 아래 "`report_loaded_for`는 성공했을 때만 세운다" 단락도 함께 바뀐다 — 이유는 이
> 섹션 끝의 정정을 참고한다.

> `[재-정정 — #647, 2026-08-10]` 표식 **하나로는 안 된다.** 위 표는 문구를
> `report_loaded_for`로 고르는데, 그 표식은 본문이 있을 때만 세워지므로 리포트가
> **정말 없는** 실험(이 변경 이전에 완주한 실험 전부)은 영영 세워지지 않는다. 결과는
> "리포트를 불러오는 중입니다."에 영구 고착이고 — `PROMOTED`는 terminal이라 폴링까지
> 멈춰 그 문구에서 내려오지 못한다 — 바로 앞 정정이 고친 버그의 정확한 역방향이다.
> PR #650 리뷰가 잡았다.
>
> **표식을 둘로 나눈다.** 서로 다른 질문에 답하기 때문이다.
>
> - `report_checked_for` — "한 번이라도 조회해 봤는가". 본문 유무와 무관하게 조회가
>   성공하면 세운다. **문구 선택에만** 쓴다.
> - `report_loaded_for` — "캐시할 본문을 이미 받았는가". 본문이 있을 때만 세우며
>   재조회를 막는다.
>
> 둘 다 실패에는 세우지 않는다. 이렇게 하면 `PASSED`의 자기 치유(재조회)를 유지하면서
> 문구는 "아직 리포트가 없습니다."로 정확해진다.
>
> | 상태 | 화면 |
> | --- | --- |
> | `report_error is not None` | `st.warning` (fetch 실패) |
> | `report_checked_for != selected_id` | caption "리포트를 불러오는 중입니다." |
> | `report_checked_for == selected_id`, 본문 `None` | caption "아직 리포트가 없습니다" |
> | 본문 있음 | `st.iframe` |

`report_error`가 최우선이다. 실패했는데 "리포트가 없습니다"로 보이면 **없는 것과 못 받은
것이 구별되지 않는다.**

### 조회 시점

`report_loaded_for != selected_id`이고 `status`가 `PASSED` 또는 `PROMOTED`일 때만 1회.
리포트는 한 번 쓰이고 변하지 않으므로 5초 polling에 태우지 않는다.

**이 두 상태가 리포트를 가진 실험의 전부다.** 전이 그래프가 그것을 보장한다.

- `record_experiment_result`가 `report_markdown`의 유일한 기록자이고, 도달 상태로
  `ExperimentStatus.PASSED`를 하드코딩한다(결정 1). 호출자가 상태를 고를 수 없다.
- `ALLOWED_TRANSITIONS[PASSED] = frozenset({PROMOTED})` — PASSED에서 나가는 간선은
  하나뿐이다. **`PASSED → FAILED`는 없다.**
- `FAILED`는 `EVALUATING`에서만 도달하고, 그 경로에는 리포트를 싣는 호출이 없다.

따라서 "완주했는데 판정이 갈려 리포트가 안 보이는" 실험은 생기지 않는다. 이 성질은
전이 그래프에 의존하므로, 나중에 `PASSED → FAILED`나 리포트를 싣는 다른 전이가 생기면
이 조건도 함께 넓혀야 한다.

**`report_loaded_for`는 성공했을 때만 세운다.** 실패에도 세우면 일시적 503 한 번에
그 세션 동안 리포트가 영구히 가려진다. `PASSED`는 terminal이 아니라 polling 상태이므로
(`TERMINAL_STATUSES`는 FAILED·ERROR·PROMOTED뿐) 다음 갱신에서 자연히 재시도되고 스스로
낫는다. `PROMOTED`는 terminal이라 `record_terminal_refresh`가 폴링을 끝내므로 재시도가
무한히 돌지 않는다 — 자기 치유와 상한이 이미 있는 구조에서 나온다.

> `[정정 — #647, 2026-08-10]` 위 단락의 "성공"은 원래 "HTTP 조회가 에러 없이 끝남"만
> 뜻했고, 본문이 `None`이어도 그것을 조회 결과로 보아 표식을 세웠다. 최종 리뷰(I3)가
> 이것이 결정 2와 만나 고착을 만든다는 것을 지적해 뒤집는다.
>
> 결정 2는 지표 커밋과 리포트 커밋을 **별도 트랜잭션**으로 나눈다. 그 결과
> `record_candidate`가 지표 트랜잭션을 커밋해 `status`가 이미 `PASSED`로 보이는데
> `_store_report_markdown`의 두 번째 트랜잭션은 아직 커밋 전인 순간이 실재한다. 그
> 틈에 5초 polling이 리포트 조회 endpoint를 때리면 실험은 있고 리포트는 아직
> 없으므로 정상적으로 **200 + `null`**이 온다(결정 1의 계약대로). 이전 규칙대로
> `report_loaded_for`를 세우면, UI는 이 null이 "진짜 리포트 없음"인지 "아직 두 번째
> 트랜잭션 전"인지 구별할 방법이 없는 채로 **표식을 영구 고착**시킨다. 이후
> `refresh_report`는 `report_loaded_for == selected_id`를 보고 매번 early-return하고,
> 결과 탭은 리포트가 실제로 도착한 뒤에도 "아직 리포트가 없습니다."에 멈춘다.
> `select_experiment`는 같은 id를 다시 선택하면 no-op이라, 사이드바에서 같은 실험을
> 다시 눌러도 이 고착은 풀리지 않는다.
>
> **바뀐 규칙:** `record_report`는 본문이 `None`이 아닐 때만 `report_loaded_for`를
> 세운다. `report_error`를 지우는 동작(조회 자체는 성공했으므로)은 그대로 둔다.
> `PASSED`가 terminal이 아니라는 성질은 그대로 재활용된다 — 표식이 서지 않으면 다음
> polling에서 자연히 재조회되어 리포트가 실제로 커밋되는 순간 스스로 낫는다.
> `PROMOTED`는 여전히 terminal이라 `record_terminal_refresh`가 폴링을 끝낸다.
>
> **트레이드오프.** 리포트를 끄고 돌린 배포(executor가 `report_markdown`을 아예
> 싣지 않는 경우)에서는 `PASSED`(또는 `PROMOTED` 진입 전 마지막 polling)인 실험이
> 화면에 선택돼 있는 동안 표식이 영영 서지 않아 5초마다 null 조회가 반복된다. 응답이
> 작아(`{experiment_id, report_markdown: null}`) 비용은 낮지만, 나중에 "왜 이 실험만
> 계속 조회하지"라는 질문이 나올 수 있다 — 이 문단이 그 답이다.

### 잡는 위치

`ui/state.py`의 docstring이 `[비책임] HTTP 요청, API 인증`을 명시한다.
`select_experiment`는 순수 함수이며 여기에 네트워크 호출을 넣지 않는다 — 그 경계가
깨지고 순수 함수 테스트도 불가능해진다. `select_experiment`가 하는 일은 세 필드를
초기화하는 것까지다.

잡는 자리는 `ui/app.py`의 `refresh_selected_experiment`이고, metadata 블록
(`app.py:163-170`)이 이미 같은 모양이다 — `metadata_loaded_for != selected_id`일 때만
1회 조회하고 `ExperimentApiError`를 `record_detail_error`로 흡수한다. 리포트는 그것을
따르되 **한 군데를 다르게** 한다.

```
    record_terminal_refresh(state)
    state.detail_error = None
    state.last_updated_at = datetime.now(timezone.utc)
    refresh_report(client, state)   # 맨 끝. 무엇이 나도 위 결과를 바꾸지 않는다
    return False
```

`refresh_report`는 실패를 **`report_error`에만** 담는다. `detail_error`를 건드리지 않고,
`remove_selected_experiment`를 부르지 않고, `return False`로 갱신을 중단시키지 않는다.

**근거:** metadata는 실패 시 갱신 전체를 접지만 리포트는 그러면 안 된다. 결정 2가 서버에서
정한 비대칭을 UI에서도 그대로 지키는 것이고, 그러지 않으면 리포트 조회 하나가 5초마다
워크벤치 전체를 오류 상태로 만든다. `ApiNotFoundError`도 여기서는 실험 제거로 올리지
않고 `report_error`로 흡수한다 — 실험이 정말 없다면 바로 앞의 `get_experiment`가 이미
그렇게 처리한 뒤다.

## 이슈 #647 본문과 달라진 것

> `[정정 — #647, 2026-08-10]` 이슈 본문의 아래 네 항목은 코드 실측과 어긋나 이 문서에서
> 뒤집는다. 원문은 이슈에 그대로 남긴다.

1. **"seed별 delta가 `metric_summary`에서 그려진다"는 지금 계약으로 불가능하다.**
   스냅샷에는 `per_seed`가 없다(결정 6). 완료 조건에서 뺀다.
2. **"`components.html`은 별도 origin iframe이라 `sandbox`와 함께 2차 방어가 된다"는
   사실이 아니다.** srcdoc이고 sandbox 고정 목록에 `allow-same-origin`과
   `allow-scripts`가 둘 다 있다(결정 5). 게다가 1.60에서 deprecated라 `st.iframe`으로
   간다.
3. **"`report_markdown` 컬럼 추가"만으로는 목록 비용이 사라지지 않는다.** `deferred`가
   함께 필요하다(결정 1).
4. **"`ExecutorResultReportRequest`에 `MAX_REPORT_MARKDOWN_BYTES` 검증"을 거절로 두면
   안 된다.** 정규화로 두고 service가 한다(결정 3). 트랜잭션 경계도 함께 분리한다
   (결정 2).

`markdown-it-py`는 이미 `uv.lock`에 transitive로 들어와 있어 그룹에 직접 선언만 하면
된다. `streamlit`도 이미 1.60.0으로 고정돼 있어 하한 상향에 설치 변동이 없다.

## 범위 밖

- GCS `report.html` 게시와 링크 공유 (변환이 UI에 있으므로 지금은 불가)
- iframe 자동 높이
- Step API 배선 (`experiment_steps`는 여전히 전 실험 공백)
- 과거 실험 리포트 백필 — GCS를 읽어야 하고, 그 경로를 만들지 않는 것이 결정 4다.
  `report_markdown`은 이 변경 이후 완주한 실험부터 채워진다.
- `_render_metrics`(inspector 패널) 개선
- `results_uri`를 링크로 만드는 것
- **이슈 #647에 정정 코멘트 올리기** — 저장소 관례(`[정정 — #647, 2026-08-10]`
  태그)를 따르는 코멘트 문안은 준비돼 있으나, GitHub에 쓰는 바깥 동작이라 사용자
  승인 없이는 올리지 않는다. 계획(`docs/plans/2026-08-10-experiment-report-html-workbench.md`)
  Task 9 Step 4가 그래서 미체크로 남아 있다.

## 검증

- **`ui/report.py`** (순수 함수) — raw HTML escape(인라인·블록), `javascript:`/
  `data:text/html` 링크 무력화, 빈 입력, 템플릿에 `<script>`가 없음
- **`executor/report.py`** — `truncate_report_markdown`의 상한 경계, 멀티바이트 문자가
  상한에 걸치는 경우, 잘리지 않았을 때 문구가 붙지 않음
- **상한 상수 일치** — executor와 schemas의 `MAX_REPORT_MARKDOWN_BYTES`가 같은 값
- **service** — 리포트 없는 보고(회귀), 리포트 있는 보고, 재시도 write-once + WARNING,
  NUL 제거·절단 정규화, **리포트 쓰기가 터져도 지표 커밋이 남는다**
- **조회 endpoint** — 실험 없음 404, 리포트 없음 200 + null, 인증 없음 401
- **UI 배선** (`streamlit.testing.v1.AppTest`) — 다섯 조합에서 탭이 살아 있다:
  지표만 / 리포트만 / 둘 다 / 둘 다 없음 / **fetch 실패**
- **UI 조회 시점** — `PASSED` 이전 상태에서는 조회하지 않는다, 성공 시 1회로 그친다,
  **실패 후 다음 갱신에서 재시도한다**, 리포트 조회 실패가 `detail_error`를 세우거나
  갱신을 중단시키지 않는다
- **executor** — 리포트가 있으면 본문이 `report_result` payload에 실린다, 리포트가
  없으면 키 자체가 없다, **본문 읽기가 실패해도 지표 보고는 나간다**
- `uv run python -m pytest`
- `uv run --no-sync ruff check agent_orchestration autoresearch tests tools`

### 실측 결과 (2026-08-10)

워크트리 루트(`C:\Users\travi\dev\Autoresearch-worktrees\feat-647-report-html`)에서
`uv run python -m pytest`를 구현 전후로 각각 돌렸다.

| | passed | failed | skipped |
| --- | --- | --- | --- |
| 구현 전 baseline | 2419 | 121 | 25 |
| 구현 후 | 2460 | 121 | 25 |

신규 테스트 41건 — `tests/test_experiment_report_api.py` 18건 +
`tests/test_agent_orchestration_ui_report.py` 23건. 증가분(41)이 신규 테스트 수와
정확히 일치하고 실패 건수는 121로 불변이라 **회귀가 없다.** 121건의 기존 실패는
Windows 개발 환경 고유(cp949 인코딩, `WinError 2` subprocess, 셸 스크립트 계열)이며
여러 Task에서 `git stash` 대조로 이 변경과 무관함을 확인했다.

`uv run --no-sync ruff check agent_orchestration autoresearch tests tools` →
`All checks passed!`.

커밋(분기점 이후 코드 8개, 문서 1개 — Refs #647):

```
eb64bad feat: 실험에 리포트 본문 컬럼을 deferred로 추가한다
2ce2446 feat: 완주 보고에 리포트 본문을 실어 별도 트랜잭션으로 적재한다
1d28058 feat: 실험 리포트 조회 endpoint를 낸다
eeb6886 feat: executor가 완주 보고에 리포트 본문을 싣는다
c4e1cb6 chore: orchestration-ui에 markdown-it-py를 더하고 streamlit 하한을 올린다
8cf8cdd feat: 리포트 md를 우리 템플릿의 HTML 페이지로 변환한다
e30e3cf feat: 워크벤치가 리포트 본문을 실험당 한 번 받아 둔다
aaa9492 feat: 결과 탭에 지표 카드와 리포트 HTML을 그린다
```

### 아직 확인하지 못한 것

- **실제 화면을 띄워 본 적이 없다.** Streamlit 워크벤치를 실행해 리포트가 실제로
  렌더되는 것을 눈으로 본 검증은 없다. `AppTest`로 다섯 조합(지표만 / 리포트만 /
  둘 다 / 둘 다 없음 / fetch 실패)에서 예외가 없음을 확인한 것이 전부다.
- **PostgreSQL에서 migration `0006`을 적용해 본 적이 없다.** 테스트는 SQLite
  in-memory로 돈다. 결정 2가 근거로 드는 NUL 거부와 `UndefinedColumn`은 실측
  재현이 아니라 주입한 예외로 성질만 검증했다.
- **완주한 실제 실험으로 끝에서 끝까지 돌려 본 적이 없다.** `report_markdown`은 이
  변경 이후 완주한 실험부터 채워지므로, 데모에서 보려면 실험을 새로 한 번 돌려야
  한다.

### 구현 중 드러난 계획의 결함

1. 계획(Task 2)의 `_evaluating_experiment` 테스트 헬퍼는 `record_candidate`의
   전제(봉인 좌표 + `RUNNING` 상태)를 빠뜨려 그대로는 실행 불가였다. 기존
   `_running_experiment` 패턴으로 보정해 구현했다.
2. 계획(Task 3)의 HTTP 테스트는 `client`와 `db_session` fixture를 함께 썼는데 둘은
   서로 다른 in-memory DB라 그대로 두면 전부 404가 났을 것이다. 기존
   `_create_evaluating_experiment_for_http` 패턴(client의 session factory 경유)으로
   고쳐 구현했다.
3. **알려진 한계.**
   `tests/test_agent_orchestration_ui_report.py`의
   `test_javascript_links_are_neutralized` 첫 단언은 assert-nothing이다 —
   `rendered.replace("javascript:alert(1)", "")` 후 검사라 항등 함수여도 통과한다.
   실질 방어는 둘째 줄(`'<a href="javascript:' not in rendered`)이 검증한다. 계획
   원문의 결함이며, 코드는 고치지 않았다.

   > `[정정 — #647, 2026-08-10]` 최종 리뷰(M4)가 이 assert-nothing을 지적해 고쳤다.
   > 첫 단언을 `assert "<a" not in rendered`(앵커 자체가 만들어지지 않았음을 직접
   > 확인)로 바꿨고, 둘째 줄은 그대로 남겼다. 더 이상 알려진 한계가 아니다.
4. **알려진 한계.**
   `test_report_statuses_are_exactly_the_states_that_can_hold_a_report`는 상수
   리터럴(`{"PASSED", "PROMOTED"}`)을 되풀이하는 스냅샷 테스트라, 근거인
   `ALLOWED_TRANSITIONS`와 코드로 연결돼 있지 않다. 전이 그래프가 바뀌어도 이
   테스트는 스스로 실패를 알리지 못한다. 계획 원문의 설계이며, 코드는 고치지
   않았다.
