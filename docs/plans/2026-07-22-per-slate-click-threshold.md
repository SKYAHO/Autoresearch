# 슬레이트당 최대 1클릭 · 관련성 커트라인 구현 계획 (#255)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development(권장) 또는 superpowers:executing-plans로 이 계획을 Task 단위로 구현한다. 각 Step은 체크박스(`- [ ]`)로 추적하고, 각 Task는 TDD, 최종 통합은 verification-before-completion을 적용한다.

**Goal:** action log 클릭 선정을 전역 CTR 할당량(`target_ctr`)에서 "유저(슬레이트)당 최고 1개 + 관련성 커트라인(`click_threshold`)"으로 완전 교체해, CTR이 모델 실력에 따라 움직이게 한다.

**Architecture:** 순수 선정 함수 `select_clicks_per_slate(drafts, click_threshold)`를 먼저 TDD로 만들고, 두 소비처(`expand_action_log_drafts`, `simulate_policy_round`)를 이 함수로 이전한다. 계약 이름 `target_ctr → click_threshold`는 "새 필드 추가 → 소비처 이전 → 옛 필드·옛 함수 제거" 순서로 갈아끼워 모든 Task가 green을 유지한다.

**Tech Stack:** Python 3.12, Pydantic v2, pytest, uv.

## Global Constraints

- 브랜치는 이슈 #255에서 만든 `feat/255-per-slate-click-threshold`를 사용한다.
- LLM 프롬프트·응답 포맷(`llm_generator.py`), `derive_would_like`(임계 0.7/0.6), watch_time/like 파생, 노출 조립(`model_exposure_provider`)은 **변경하지 않는다.**
- `target_ctr`와 `click_threshold`를 영구 공존시키지 않는다(마지막 Task에서 `target_ctr` 제거).
- 클릭 커트라인 기본값은 `0.55`(설계 문서의 0.5~0.6 출발점). 실제 캘리브레이션 값 확정은 별도(기본 모델 1회 실행)이며 이 계획의 코드 범위 밖이다.
- 커트라인 비교는 **이상(≥)** 이다. 동점은 `(-click_propensity, video_id)`로 결정적으로 깬다.
- 단일 출처: `click_threshold` 기본값·상수는 `autoresearch/action_logs`가 소유하고 `simulate_policy_round`는 거기서 가져온다.
- 설계 문서: `docs/specs/2026-07-22-per-slate-click-threshold.md`

---

## 파일 구조

| 파일 | 책임 | 변경 |
| --- | --- | --- |
| `autoresearch/action_logs/pipeline.py` | 클릭 선정 알고리즘·이벤트 확장 | 새 `select_clicks_per_slate` 추가, `expand_action_log_drafts` 소비처 교체, 옛 `_clicked_indices`/`normalize_clicks` 제거 |
| `autoresearch/action_logs/schema.py` | 요청·manifest 데이터 계약 | `click_threshold` 추가 → `target_ctr` 제거 |
| `autoresearch/action_logs/daily.py` | 일일 생성 스레딩 | `target_ctr` → `click_threshold` |
| `autoresearch/jobs/action_log.py` | 공개 CLI | `--target-ctr` → `--click-threshold` |
| `src/pipeline/simulate_policy_round.py` | 정책 시뮬 클릭 선정 | 전역 정규화 → per-slate, `target_ctr` → `click_threshold` |
| `src/pipeline/report_html.py` | 리포트 표기 | `target_ctr` → `click_threshold` |
| `tests/test_action_logs_pipeline.py` | 클릭 선정 테스트 | 새 계약으로 교체·추가 |
| `tests/test_action_logs_daily.py` | 일일 스레딩 테스트 | 필드명 갱신 |
| `tests/test_simulate_policy_round.py` | 정책 시뮬 테스트 | 새 계약으로 교체 |
| `docs/guides/action-log.md`, `docs/guides/agent-simulator-spec.md`, `docs/specs/2026-07-20-policy-simulation-round.md`, `docs/specs/2026-07-22-daily-closed-loop.md` | 살아있는 권위 문서 | 새 클릭 계약 반영 |

---

## Task 1: 순수 per-slate 클릭 선정 함수

**Files:**
- Modify: `autoresearch/action_logs/pipeline.py`
- Test: `tests/test_action_logs_pipeline.py`

**Interfaces:**
- Consumes: `ImpressionDraft`(`user_id`, `video_id`, `click_propensity` 보유)
- Produces: `select_clicks_per_slate(drafts: list[ImpressionDraft], click_threshold: float) -> set[int]`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_action_logs_pipeline.py`에 추가한다.

```python
from autoresearch.action_logs.pipeline import select_clicks_per_slate
from autoresearch.action_logs.schema import ImpressionDraft


def _draft(user_id: str, video_id: str, cp: float) -> ImpressionDraft:
    return ImpressionDraft(
        user_id=user_id,
        video_id=video_id,
        click_propensity=cp,
        watch_fraction=0.5,
        would_like=False,
        duration_sec=100,
    )


def test_select_clicks_one_top_per_user_above_threshold() -> None:
    drafts = [
        _draft("u1", "a", 0.30),
        _draft("u1", "b", 0.80),  # u1 최고 → 클릭
        _draft("u2", "c", 0.40),  # u2 최고지만 커트라인 미만 → 클릭 없음
        _draft("u2", "d", 0.20),
    ]
    assert select_clicks_per_slate(drafts, 0.55) == {1}


def test_select_clicks_none_when_all_below_threshold() -> None:
    drafts = [_draft("u1", "a", 0.10), _draft("u1", "b", 0.20)]
    assert select_clicks_per_slate(drafts, 0.55) == set()


def test_select_clicks_threshold_is_inclusive() -> None:
    drafts = [_draft("u1", "a", 0.55)]
    assert select_clicks_per_slate(drafts, 0.55) == {0}


def test_select_clicks_tiebreak_is_deterministic_by_video_id() -> None:
    drafts = [_draft("u1", "b", 0.80), _draft("u1", "a", 0.80)]
    # 동점이면 video_id 작은 "a"(index 1)가 선택된다.
    assert select_clicks_per_slate(drafts, 0.55) == {1}


def test_select_clicks_handles_empty() -> None:
    assert select_clicks_per_slate([], 0.55) == set()
```

- [ ] **Step 2: 실패 확인**

Run: `uv run --no-sync python -m pytest tests/test_action_logs_pipeline.py -k select_clicks -v`
Expected: FAIL (`ImportError: cannot import name 'select_clicks_per_slate'`)

- [ ] **Step 3: 최소 구현**

`autoresearch/action_logs/pipeline.py`의 `_clicked_indices` 근처(677행 부근)에 추가한다. **옛 `_clicked_indices`/`normalize_clicks`는 이 Task에서 지우지 않는다**(Task 5에서 제거).

```python
def select_clicks_per_slate(
    drafts: list[ImpressionDraft], click_threshold: float
) -> set[int]:
    """유저(슬레이트)별 click_propensity 최고 1개가 커트라인 이상이면 그 draft
    인덱스를 클릭으로 선정한다. 최고가 커트라인 미만이면 그 유저는 클릭 0개.

    동점은 (-click_propensity, video_id)로 결정적으로 깬다(높은 점수 우선,
    같으면 video_id 작은 쪽). 전역 할당량이 아니라 관련성 커트라인이므로
    CTR은 점수 분포(모델 실력)에 따라 창발한다.
    """
    indices_by_user: dict[str, list[int]] = {}
    for index, draft in enumerate(drafts):
        indices_by_user.setdefault(draft.user_id, []).append(index)

    clicked: set[int] = set()
    for indices in indices_by_user.values():
        top = min(
            indices,
            key=lambda i: (-drafts[i].click_propensity, drafts[i].video_id),
        )
        if drafts[top].click_propensity >= click_threshold:
            clicked.add(top)
    return clicked
```

- [ ] **Step 4: 통과 확인**

Run: `uv run --no-sync python -m pytest tests/test_action_logs_pipeline.py -k select_clicks -v`
Expected: PASS (5개)

- [ ] **Step 5: 커밋**

```bash
git add autoresearch/action_logs/pipeline.py tests/test_action_logs_pipeline.py
git commit -m "feat: #255 per-slate 클릭 선정 함수 추가"
```

---

## Task 2: action-log 경로를 per-slate 선정으로 이전 + click_threshold 추가

**Files:**
- Modify: `autoresearch/action_logs/schema.py:171` (`EventGenerationRequest`), `:126` (`ActionLogShardManifest`), `:189` (validator)
- Modify: `autoresearch/action_logs/pipeline.py:1082` (`expand_action_log_drafts`), `:1046` (logger extra)
- Test: `tests/test_action_logs_pipeline.py`

**Interfaces:**
- Consumes: Task 1의 `select_clicks_per_slate`
- Produces: `EventGenerationRequest.click_threshold: float = 0.55`, `ActionLogShardManifest.click_threshold`
- Constraint: 이 Task에서 `target_ctr`는 **남겨둔다**(파괴적 제거는 Task 5). 두 필드 공존은 backward-safe.

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_action_logs_pipeline.py`에 추가한다.

```python
from autoresearch.action_logs.pipeline import expand_action_log_drafts
from autoresearch.action_logs.schema import EventGenerationRequest


def test_expand_uses_per_slate_click_threshold() -> None:
    request = EventGenerationRequest(click_threshold=0.55)
    drafts = [
        _draft("u1", "a", 0.80),  # 클릭
        _draft("u1", "b", 0.30),
        _draft("u2", "c", 0.40),  # 커트라인 미만 → 클릭 없음
    ]
    result = expand_action_log_drafts(request, drafts)
    clicks = [e for e in result.batch.events if e.event_type == "click"]
    assert {c.video_id for c in clicks} == {"a"}


def test_event_generation_request_defaults_click_threshold() -> None:
    assert EventGenerationRequest().click_threshold == 0.55
```

- [ ] **Step 2: 실패 확인**

Run: `uv run --no-sync python -m pytest tests/test_action_logs_pipeline.py -k "per_slate_click_threshold or defaults_click_threshold" -v`
Expected: FAIL (`click_threshold` 필드 없음)

- [ ] **Step 3: 스키마에 click_threshold 추가**

`autoresearch/action_logs/schema.py`:

`EventGenerationRequest`(171행 `target_ctr: float = 0.02` 아래)에 추가:

```python
    click_threshold: float = 0.55
```

`field_validator`(189행)의 검증 대상 튜플에 `"click_threshold"`를 추가:

```python
    @field_validator(
        "target_ctr",
        "click_threshold",
        "personalized_ratio",
        "popular_ratio",
        "exploration_ratio",
        "max_quarantine_ratio",
    )
```

`ActionLogShardManifest`(126행 `target_ctr: float = ...` 아래)에 추가:

```python
    click_threshold: float = Field(default=0.55, ge=0.0, le=1.0)
```

- [ ] **Step 4: pipeline 소비처 교체**

`autoresearch/action_logs/pipeline.py`의 `expand_action_log_drafts`(1082행):

```python
    clicked = select_clicks_per_slate(drafts, request.click_threshold)
```

docstring(1080행)의 "전역 CTR 정규화"를 "유저별 커트라인 클릭 선정"으로 바꾼다. logger extra(1046행)의 `"target_ctr": request.target_ctr`를 `"click_threshold": request.click_threshold`로 바꾼다.

- [ ] **Step 5: 통과 확인**

Run: `uv run --no-sync python -m pytest tests/test_action_logs_pipeline.py -v`
Expected: PASS (기존 + 신규). 전역 정규화 전제로 깨지는 기존 테스트가 있으면 per-slate 계약으로 수정한다.

- [ ] **Step 6: 커밋**

```bash
git add autoresearch/action_logs/schema.py autoresearch/action_logs/pipeline.py tests/test_action_logs_pipeline.py
git commit -m "feat: #255 action-log 클릭을 per-slate 커트라인으로 이전"
```

---

## Task 3: 일일 생성·CLI를 click_threshold로 이전

**Files:**
- Modify: `autoresearch/action_logs/daily.py` (target_ctr 참조: 546, 739, 783, 796, 824, 903, 976, 1087, 1206행)
- Modify: `autoresearch/jobs/action_log.py` (CLI arg 및 296, 321행)
- Test: `tests/test_action_logs_daily.py`

**Interfaces:**
- Consumes: Task 2의 `EventGenerationRequest.click_threshold`
- Produces: CLI `--click-threshold`(기본 0.55), daily 경로가 `click_threshold`를 스레딩

- [ ] **Step 1: 실패 테스트 작성/수정**

`tests/test_action_logs_daily.py`에서 `target_ctr`을 쓰던 단정을 `click_threshold`로 바꾸고, CLI 파싱 테스트를 추가한다.

```python
def test_cli_parses_click_threshold() -> None:
    from autoresearch.jobs.action_log import _build_parser

    args = _build_parser().parse_args(["--click-threshold", "0.6"])
    assert args.click_threshold == 0.6
```

- [ ] **Step 2: 실패 확인**

Run: `uv run --no-sync python -m pytest tests/test_action_logs_daily.py -v`
Expected: FAIL

- [ ] **Step 3: daily.py 스레딩 rename**

`autoresearch/action_logs/daily.py`의 target_ctr 참조 9곳(위 라인)을 `click_threshold`로 바꾼다. 함수 인자 기본값 `target_ctr: float = 0.02`는 `click_threshold: float = 0.55`로, `target_ctr=...` 전달은 `click_threshold=...`로, `request.target_ctr`/`manifest.target_ctr`는 `.click_threshold`로 바꾼다. 값의 흐름(호출→요청→manifest)은 그대로 유지한다.

- [ ] **Step 4: CLI rename**

`autoresearch/jobs/action_log.py:133`의
`parser.add_argument("--target-ctr", type=_ratio, default=0.02)`를
`parser.add_argument("--click-threshold", type=_ratio, default=0.55)`로 바꾸고,
296·321행의 `target_ctr=args.target_ctr`를 `click_threshold=args.click_threshold`로 바꾼다.

- [ ] **Step 5: 통과 확인**

Run: `uv run --no-sync python -m pytest tests/test_action_logs_daily.py -v`
Expected: PASS

- [ ] **Step 6: 커밋**

```bash
git add autoresearch/action_logs/daily.py autoresearch/jobs/action_log.py tests/test_action_logs_daily.py
git commit -m "feat: #255 일일 생성·CLI를 click_threshold로 이전"
```

---

## Task 4: 정책 시뮬·리포트를 per-slate 선정으로 이전

**Files:**
- Modify: `src/pipeline/simulate_policy_round.py` (134행 인자, 193·213·293행)
- Modify: `src/pipeline/report_html.py:127`
- Test: `tests/test_simulate_policy_round.py`

**Interfaces:**
- Consumes: Task 1의 `select_clicks_per_slate`
- Produces: 정책 시뮬이 per-slate 커트라인으로 클릭 선정, 리포트가 `click_threshold` 표기

- [ ] **Step 1: 실패 테스트 작성/수정**

`tests/test_simulate_policy_round.py`의 기존 3개 테스트
(`test_round_report_prefers_model_policy_when_model_is_right`,
`test_round_events_are_tagged_per_policy`,
`test_round_output_feeds_retraining_path`)에서 `main(...)` 인자 `target_ctr=0.2`를
`click_threshold=0.0`으로 바꾼다(커트라인 0 → 유저별 최고 1개는 항상 클릭되어
기존 단정 유지). 그리고 유저별 클릭 최대 1개를 고정하는 회귀를 추가한다:

```python
def test_round_clicks_are_at_most_one_per_user(tmp_path, stub_reranker) -> None:
    import pyarrow.parquet as pq

    main(
        personas=_personas(),
        virtual_users=_virtual_users(),
        videos_raw=_videos_raw(),
        events=_empty_events(),
        generator=RuleBasedActionLogGenerator(),
        reranker=stub_reranker,
        k=6,
        exploration_ratio=0.0,
        click_threshold=0.0,
        seed=42,
        policy_version="stub-run",
        output_dir=str(tmp_path),
    )
    table = pq.read_table(tmp_path / "event_log.parquet").to_pandas()
    clicks = table[table["event_type"] == "click"]
    per_user = clicks.groupby(["policy", "user_id"]).size()
    assert (per_user <= 1).all()
```

- [ ] **Step 2: 실패 확인**

Run: `uv run --no-sync python -m pytest tests/test_simulate_policy_round.py -v`
Expected: FAIL

- [ ] **Step 3: simulate 교체**

`src/pipeline/simulate_policy_round.py`:
- 213행 `for i in normalize_clicks(draft_result.drafts, target_ctr)` →
  `for i in select_clicks_per_slate(draft_result.drafts, click_threshold)`
- import를 `from autoresearch.action_logs.pipeline import select_clicks_per_slate`로 교체(35행 `normalize_clicks` import 제거)
- 134행 인자 `target_ctr: float = 0.02` → `click_threshold: float = 0.55`
- 193·293행의 `target_ctr` 전달·표기를 `click_threshold`로 변경

- [ ] **Step 4: report_html 교체**

`src/pipeline/report_html.py:127`의 `target_ctr={report["target_ctr"]}`를 `click_threshold={report["click_threshold"]}`로 바꾸고, 이 값을 채우는 상류 dict 키도 함께 변경한다.

- [ ] **Step 5: 통과 확인**

Run: `uv run --no-sync python -m pytest tests/test_simulate_policy_round.py -v`
Expected: PASS

- [ ] **Step 6: 커밋**

```bash
git add src/pipeline/simulate_policy_round.py src/pipeline/report_html.py tests/test_simulate_policy_round.py
git commit -m "feat: #255 정책 시뮬·리포트를 per-slate 커트라인으로 이전"
```

---

## Task 5: 옛 전역 정규화 제거 + 살아있는 문서 갱신

**Files:**
- Modify: `autoresearch/action_logs/pipeline.py` (`_clicked_indices`/`normalize_clicks` 제거)
- Modify: `autoresearch/action_logs/schema.py` (`target_ctr` 필드 제거)
- Modify: `docs/guides/action-log.md`, `docs/guides/agent-simulator-spec.md`, `docs/specs/2026-07-20-policy-simulation-round.md`, `docs/specs/2026-07-22-daily-closed-loop.md`

**Interfaces:**
- Consumes: Task 2~4로 `target_ctr`·옛 함수 소비처가 모두 사라진 상태
- Produces: `target_ctr`·전역 정규화가 코드에서 완전히 제거됨

- [ ] **Step 1: 잔존 참조 0 확인**

Run: `grep -rn "target_ctr\|normalize_clicks\|_clicked_indices" autoresearch/ src/ tests/`
Expected: **매치 없음**. 남아 있으면 해당 소유 Task로 돌아가 정리한다.

- [ ] **Step 2: 옛 함수·필드 제거**

`autoresearch/action_logs/pipeline.py`에서 `_clicked_indices`, `normalize_clicks`(677~694행) 제거. `autoresearch/action_logs/schema.py`에서 `EventGenerationRequest.target_ctr`(171행), `ActionLogShardManifest.target_ctr`(126행), validator 튜플의 `"target_ctr"`(190행) 제거.

- [ ] **Step 3: 전체 테스트**

Run: `uv run --no-sync python -m pytest tests/test_action_logs_pipeline.py tests/test_action_logs_daily.py tests/test_simulate_policy_round.py -v`
Expected: PASS

- [ ] **Step 4: 살아있는 문서 갱신**

네 문서에서 "전역 2% 정규화/target_ctr" 설명을 "유저별 최고 1개 + 관련성 커트라인(click_threshold)"으로 바꾼다. **`docs/archive/**`와 PR 리포트 HTML은 손대지 않는다**(동결). CTR이 창발 지표가 됨을 명시한다.

- [ ] **Step 5: 커밋**

```bash
git add autoresearch/action_logs/pipeline.py autoresearch/action_logs/schema.py docs/guides/action-log.md docs/guides/agent-simulator-spec.md docs/specs/2026-07-20-policy-simulation-round.md docs/specs/2026-07-22-daily-closed-loop.md
git commit -m "refactor: #255 전역 CTR 정규화 제거 + 문서 갱신"
```

---

## Task 6: 전체 회귀 검증

**Files:** 없음(발견한 결함은 소유 Task로 돌아가 실패 테스트부터 추가)

- [ ] **Step 1: dev 전체 테스트**

Run: `uv sync --frozen && uv run --no-sync python -m pytest -q`
Expected: 전체 PASS. 노출 조립·watch_time/like 파생은 불변.

- [ ] **Step 2: 정적 검증**

```bash
uv run --no-sync ruff check autoresearch tests tools
uv lock --check
git diff --check
```

Expected: 통과.

- [ ] **Step 3: 잔존 참조 재확인**

Run: `grep -rn "target_ctr\|normalize_clicks\|_clicked_indices" autoresearch/ src/ docs/guides/ docs/specs/2026-07-20-policy-simulation-round.md docs/specs/2026-07-22-daily-closed-loop.md`
Expected: 매치 없음.

## 완료 기준

- 클릭은 유저별 최대 1개이며, 최고 점수가 커트라인 미만인 유저는 클릭 0개다.
- `target_ctr`·전역 정규화(`_clicked_indices`/`normalize_clicks`)가 코드·CLI·스키마·리포트·정책 시뮬에서 제거됐다.
- CTR은 고정이 아니라 점수 분포(모델 실력)에 따라 변한다(테스트로 고정).
- 클릭된 영상의 watch_time/like 파생 동작은 불변이다.
- 살아있는 문서 4개가 새 계약으로 갱신되고, archive/리포트는 동결됐다.

## 롤백

클릭 선정 함수와 `click_threshold` 스레딩을 되돌리고 `target_ctr` 전역 정규화를 복원하면 되는 단일 개념의 역변경이다. FeatureView·BQ 테이블·노출 조립은 건드리지 않으므로 데이터 롤백은 없다.
