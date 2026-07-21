# 모델 노출 조립 provider 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `user_recommendations` dt 파티션의 champion 모델 순위를 70% 슬라이스 소스로 쓰는 24개(모델 17·트렌딩 5·랜덤 2) 노출 provider를 구현하고, 노출별 정책 태그(`exposure_source`)가 event log에 남는 계약을 만든다.

**Architecture:** `autoresearch/action_logs/pipeline.py`의 기존 `candidate_provider` seam과 `ExposureMetadata` → `_expand_events(metadata=...)` 조인 경로를 그대로 재사용한다. BQ 접근(리더)과 조립(순수 함수)·provider 팩토리는 `src/pipeline/model_exposure_provider.py` 신규 모듈에 두고, `autoresearch/action_logs/`에는 additive 스키마 필드 1개(`exposure_source`)만 추가한다. Source spec: `docs/specs/2026-07-22-model-exposure-assembly.md`.

**Tech Stack:** Python 3.12, pandas, google-cloud-bigquery(+db-dtypes), pyarrow, pydantic, pytest.

## Global Constraints

- 브랜치: `feat/221-model-exposure-assembly` (이슈 #221에서 생성).
- 한국어 격식체 docstring, 모든 함수 타입 힌트(반환 포함).
- `autoresearch/action_logs/`는 BQ 비의존 순수 유지 — BQ 리더는 `src/pipeline/`.
- 스키마 확장은 additive optional (`None` 기본) — 기존 historical 로그 하위 호환.
- **함정 1 (메모리 기록)**: additive 스키마 확장은 정확 동등 단언과 충돌한다 —
  `tests/test_action_logs_pipeline.py:186`의 warehouse row 키 집합 `==` 단언에
  `"exposure_source"` 추가 필수. `:182`의 `"exposure_type" not in columns` 단언은
  이름이 다르므로 유지(휴리스틱 경로는 여전히 무태그).
- **함정 2 (메모리 기록)**: 품질잡은 `EVENT_LOG_PARQUET_SCHEMA` 기준
  missing_columns를 실패 처리한다 — 구 파티션 소급 스캔이 깨지지 않도록
  `exposure_source`는 hard 검사에서 제외(OPTIONAL 집합)한다. 값 검증은
  `EventLog.model_validate` 경유로 자동(Literal).
- 슬롯 산식·셔플·seed는 기존 `build_candidates`와 동일 규약
  (`random.Random(f"{seed}:{user_id}")`는 pipeline이 주입 — provider는 받은 rng만 사용).
- dt 정합 fail-fast: 대상 dt 파티션 0행이면 RuntimeError. 휴리스틱 대체 금지(#222).
- LLM 프롬프트에 태그·점수 비노출 — 후보 dict에 메타데이터 키를 넣지 않고
  `(user_id, video_id)` 키 별도 맵으로 유지.
- 테스트는 실 BQ 미접속(fake client). 신규 모듈에 `__arch__` 사이드카 포함(#202 게이트).
- 테스트 명령: `uv run python -m pytest tests/<파일>.py -v`.

## 파일 구조 (최종)

```
autoresearch/action_logs/schema.py        # 수정: EventLog.exposure_source + to_warehouse_row (Task 1)
autoresearch/action_logs/pipeline.py      # 수정: PARQUET_SCHEMA·ExposureMetadata·_emit·_event_rows (Task 1)
autoresearch/jobs/action_log_quality.py   # 수정: OPTIONAL_ADDITIVE_COLUMNS 완화 (Task 1)
src/pipeline/model_exposure_provider.py   # 신규: RankedVideo·조립(Task 2)·리더(Task 3)·팩토리(Task 4)
tests/test_action_logs_schema_policy.py   # 수정: exposure_source 왕복·검증 (Task 1)
tests/test_action_logs_pipeline.py        # 수정: 키 집합 단언 갱신 (Task 1)
tests/test_action_log_quality_job.py      # 수정: optional 컬럼 소급 호환 (Task 1)
tests/test_model_exposure_provider.py     # 신규 (Task 2, 3, 4)
```

---

### Task 1: `exposure_source` 스키마 확장 + 품질 검사 완화

**Files:**
- Modify: `autoresearch/action_logs/schema.py` (EventLog 필드, to_warehouse_row)
- Modify: `autoresearch/action_logs/pipeline.py` (EVENT_LOG_PARQUET_SCHEMA, ExposureMetadata, `_emit`, `_event_rows`)
- Modify: `autoresearch/jobs/action_log_quality.py` (`summarize_final_schema`)
- Test: `tests/test_action_logs_schema_policy.py`, `tests/test_action_logs_pipeline.py`, `tests/test_action_log_quality_job.py`

**Interfaces:**
- Produces (Task 2·4가 사용):
  - `EventLog.exposure_source: Literal["model", "trending", "random"] | None = None`
  - `ExposureMetadata.exposure_source: Literal["model", "trending", "random"] | None = None` (frozen dataclass 말미 기본값 필드)
  - `_expand_events(..., metadata=...)`가 meta.exposure_source를 EventLog로 전파
  - 품질잡: `OPTIONAL_ADDITIVE_COLUMNS = frozenset({"exposure_source"})` — missing 검사 제외

- [x] **Step 1: 실패하는 테스트 작성** — `tests/test_action_logs_schema_policy.py`에 추가

```python
def test_exposure_source_roundtrip_and_validation():
    event = _policy_event(exposure_source="model")
    assert event.to_warehouse_row()["exposure_source"] == "model"

    legacy = _policy_event()  # 필드 미지정 — 기존 로그 하위 호환
    assert legacy.exposure_source is None
    assert legacy.to_warehouse_row()["exposure_source"] is None

    with pytest.raises(ValidationError):
        _policy_event(exposure_source="heuristic")  # 세 값 외 거부
```

(`_policy_event`는 파일 내 기존 EventLog 생성 헬퍼 — 없으면 impression 이벤트를 만드는 로컬 헬퍼를 함께 추가한다.)

- [x] **Step 2: 실패 확인**

Run: `uv run python -m pytest tests/test_action_logs_schema_policy.py -v`
Expected: FAIL — `exposure_source` unexpected keyword / 키 부재

- [x] **Step 3: 구현**

`schema.py` — `EventLog`의 `policy_version` 필드 아래에:

```python
    exposure_source: Literal["model", "trending", "random"] | None = None
```

`to_warehouse_row` 반환 dict에:

```python
            "exposure_source": self.exposure_source,
```

`pipeline.py` — ① `EVENT_LOG_PARQUET_SCHEMA`의 `policy_version` 필드 뒤에
`pa.field("exposure_source", pa.string())` 추가. ② `ExposureMetadata` 말미에
`exposure_source: Literal["model", "trending", "random"] | None = None`.
③ `_emit`의 EventLog 생성에 `exposure_source=meta.exposure_source if meta else None,`.
④ `_event_rows`의 row dict에 `"exposure_source": event.exposure_source,`.

`action_log_quality.py` — `summarize_final_schema`:

```python
OPTIONAL_ADDITIVE_COLUMNS = frozenset({"exposure_source"})  # 모듈 상수

    missing_columns = sorted(
        set(EVENT_LOG_PARQUET_SCHEMA.names) - actual_names - OPTIONAL_ADDITIVE_COLUMNS
    )
```

- [x] **Step 4: 기존 정확 동등 단언 갱신 (함정 1)**

`tests/test_action_logs_pipeline.py:186` 키 집합에 `"exposure_source"` 추가.
`tests/test_action_log_quality_job.py`에 소급 호환 테스트 추가:

```python
def test_quality_treats_exposure_source_as_optional_for_legacy_partitions():
    # exposure_source가 없는 구 파티션 스키마 — missing_columns에 포함되지 않아야 한다
    legacy_schema = pa.schema(
        [f for f in EVENT_LOG_PARQUET_SCHEMA if f.name != "exposure_source"]
    )
    summary = summarize_final_schema([], legacy_schema)
    assert "exposure_source" not in summary["action_schema_missing_columns"]
```

- [x] **Step 5: 통과 확인**

Run: `uv run python -m pytest tests/test_action_logs_schema_policy.py tests/test_action_logs_pipeline.py tests/test_action_log_quality_job.py -v`
Expected: PASS

- [x] **Step 6: Commit**

```bash
git add autoresearch/action_logs/schema.py autoresearch/action_logs/pipeline.py \
  autoresearch/jobs/action_log_quality.py tests/test_action_logs_schema_policy.py \
  tests/test_action_logs_pipeline.py tests/test_action_log_quality_job.py
git commit -m "feat: event log에 exposure_source 정책 태그 추가 (#221)"
```

---

### Task 2: 순수 조립 함수 `build_model_exposures`

**Files:**
- Create: `src/pipeline/model_exposure_provider.py`
- Test: `tests/test_model_exposure_provider.py` (신규)

**Interfaces:**
- Consumes: Task 1의 `ExposureMetadata`(from `autoresearch.action_logs.pipeline`)
- Produces (Task 3·4가 사용):
  - `@dataclass(frozen=True, slots=True) class RankedVideo: video_id: str; rank: int; ctr_score: float | None`
  - `build_model_exposures(user_id, ranking, videos, rng, *, model_run_id, candidates_per_user=24, personalized_ratio=0.7, popular_ratio=0.2, exploration_ratio=0.1) -> tuple[list[dict], dict[tuple[str, str], ExposureMetadata]]`

- [x] **Step 1: 실패하는 테스트 작성** — `tests/test_model_exposure_provider.py` (신규)

```python
"""모델 노출 조립 provider 단위 테스트 — 실 BQ 미접속(fake client)."""

import random

import pytest

from autoresearch.action_logs.pipeline import ExposureMetadata
from src.pipeline.model_exposure_provider import RankedVideo, build_model_exposures


def _videos(n: int = 40) -> list[dict]:
    return [
        {
            "video_id": f"v{i:03d}",
            "title": f"title {i}",
            "description": f"desc {i}",
            "tags": [],
            "view_count": 1000 - i,  # v000이 최고 인기
        }
        for i in range(n)
    ]


def _ranking(n: int = 30) -> list[RankedVideo]:
    # 모델 순위: v039부터 역순(인기와 어긋나게) — 슬롯 출처 구분 가능
    return [
        RankedVideo(video_id=f"v{39 - i:03d}", rank=i + 1, ctr_score=0.9 - i * 0.01)
        for i in range(n)
    ]


def _sources(meta: dict) -> dict[str, int]:
    counts: dict[str, int] = {"model": 0, "trending": 0, "random": 0}
    for m in meta.values():
        counts[m.exposure_source] += 1
    return counts


def test_default_slots_are_17_model_5_trending_2_random():
    candidates, meta = build_model_exposures(
        "u1", _ranking(), _videos(), random.Random(42), model_run_id="run-a"
    )
    assert len(candidates) == 24
    assert _sources(meta) == {"model": 17, "trending": 5, "random": 2}


def test_model_slots_follow_rank_and_carry_score_and_lineage():
    _, meta = build_model_exposures(
        "u1", _ranking(), _videos(), random.Random(42), model_run_id="run-a"
    )
    model_rows = {m.rank: m for m in meta.values() if m.exposure_source == "model"}
    assert sorted(model_rows) == list(range(1, 18))  # rank 1..17
    assert model_rows[1].ctr_score == pytest.approx(0.9)
    assert all(m.policy == "model" for m in meta.values())
    assert all(m.policy_version == "run-a" for m in meta.values())
    trending = [m for m in meta.values() if m.exposure_source == "trending"]
    assert all(m.ctr_score is None for m in trending)
    randoms = [m for m in meta.values() if m.exposure_source == "random"]
    assert all(m.is_exploration for m in randoms)


def test_trending_overlap_with_model_falls_to_next_popular():
    # 모델 상위가 인기 상위(v000~)와 겹치도록 모델 순위를 인기순과 동일하게 구성
    ranking = [
        RankedVideo(video_id=f"v{i:03d}", rank=i + 1, ctr_score=0.5) for i in range(17)
    ]
    candidates, meta = build_model_exposures(
        "u1", ranking, _videos(), random.Random(42), model_run_id="run-a"
    )
    video_ids = [str(v["video_id"]) for v in candidates]
    assert len(video_ids) == len(set(video_ids))  # 중복 없음
    trending_ids = {
        vid for (uid, vid), m in meta.items() if m.exposure_source == "trending"
    }
    assert trending_ids == {"v017", "v018", "v019", "v020", "v021"}  # 다음 인기 5개


def test_shortfall_fills_from_trending_then_random_with_true_tags():
    candidates, meta = build_model_exposures(
        "u1", _ranking(5), _videos(), random.Random(42), model_run_id="run-a"
    )
    assert len(candidates) == 24
    counts = _sources(meta)
    assert counts["model"] == 5  # 모델로 위장하지 않음
    assert counts["model"] + counts["trending"] + counts["random"] == 24


def test_user_without_recommendations_gets_trending_and_random_only():
    candidates, meta = build_model_exposures(
        "u1", [], _videos(), random.Random(42), model_run_id="run-a"
    )
    assert len(candidates) == 24
    assert _sources(meta)["model"] == 0


def test_missing_video_join_skips_to_next_rank():
    ranking = [RankedVideo(video_id="missing", rank=1, ctr_score=0.9)] + _ranking(20)
    _, meta = build_model_exposures(
        "u1", ranking, _videos(), random.Random(42), model_run_id="run-a"
    )
    assert ("u1", "missing") not in meta
    assert _sources(meta)["model"] == 17


def test_deterministic_for_same_rng_seed():
    first, _ = build_model_exposures(
        "u1", _ranking(), _videos(), random.Random("s:u1"), model_run_id="run-a"
    )
    second, _ = build_model_exposures(
        "u1", _ranking(), _videos(), random.Random("s:u1"), model_run_id="run-a"
    )
    assert [v["video_id"] for v in first] == [v["video_id"] for v in second]
```

- [x] **Step 2: 실패 확인**

Run: `uv run python -m pytest tests/test_model_exposure_provider.py -v`
Expected: FAIL — `ModuleNotFoundError: src.pipeline.model_exposure_provider`

- [x] **Step 3: 구현** — `src/pipeline/model_exposure_provider.py` (신규)

```python
"""user_recommendations 기반 모델 노출 조립 provider.

champion 모델의 유저별 순위(70%) + 트렌딩(20%) + 랜덤(10%)으로 노출 batch를
구성하고, 노출별 정책 태그(ExposureMetadata)를 별도 맵으로 유지한다 — LLM
프롬프트에는 태그·점수를 노출하지 않는다.

spec: docs/specs/2026-07-22-model-exposure-assembly.md
"""

from __future__ import annotations

__arch__ = {
    "stage": "training",
    "role": "champion 모델 순위를 일일 노출 70% 슬라이스로 조립합니다.",
    "owns": [
        "user_recommendations 파티션 리더",
        "70/20/10 노출 조립·정책 태그",
    ],
    "not_owns": [
        "LLM 판정·클릭 정규화",
        "일일 CLI 배선·cutover(#222)",
    ],
}

import logging
import random
from dataclasses import dataclass, field
from datetime import date
from typing import Sequence

from google.cloud import bigquery

from autoresearch.action_logs.pipeline import CandidateProvider, ExposureMetadata

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RankedVideo:
    """user_recommendations 1행 — 유저별 모델 순위 항목."""

    video_id: str
    rank: int
    ctr_score: float | None


def build_model_exposures(
    user_id: str,
    ranking: Sequence[RankedVideo],
    videos: Sequence[dict],
    rng: random.Random,
    *,
    model_run_id: str | None,
    candidates_per_user: int = 24,
    personalized_ratio: float = 0.7,
    popular_ratio: float = 0.2,
    exploration_ratio: float = 0.1,
) -> tuple[list[dict], dict[tuple[str, str], ExposureMetadata]]:
    """유저 1명의 노출 batch를 (후보 dict 목록, 태그 맵)으로 조립한다.

    슬롯 산식은 기존 build_candidates와 동일: popular·explore를 round로 뜨고
    나머지가 model. 부족분은 trending → random 순으로 채우되 태그는 실제 소스를
    따른다(모델로 위장 금지 — spec 부족분 규칙).
    """
    videos_by_id = {str(v.get("video_id", "")): v for v in videos if v.get("video_id")}
    if not videos_by_id:
        return [], {}

    n_total = min(candidates_per_user, len(videos_by_id))
    ratio_sum = personalized_ratio + popular_ratio + exploration_ratio
    if ratio_sum <= 0:
        personalized_ratio, popular_ratio, exploration_ratio = 1.0, 0.0, 0.0
        ratio_sum = 1.0
    n_popular = min(round(n_total * popular_ratio / ratio_sum), n_total)
    n_explore = min(round(n_total * exploration_ratio / ratio_sum), n_total - n_popular)
    n_model = n_total - n_popular - n_explore

    # (video, source, model_rank, ctr_score) — 태그는 최종 셔플 후 맵으로 변환
    selected: list[tuple[dict, str, int | None, float | None]] = []
    seen: set[str] = set()

    def take(video: dict, source: str, model_rank: int | None, ctr: float | None) -> bool:
        video_id = str(video.get("video_id", ""))
        if not video_id or video_id in seen:
            return False
        seen.add(video_id)
        selected.append((video, source, model_rank, ctr))
        return True

    skipped_joins = 0
    taken_model = 0
    for item in ranking:
        if taken_model >= n_model:
            break
        video = videos_by_id.get(item.video_id)
        if video is None:
            skipped_joins += 1
            continue
        if take(video, "model", item.rank, item.ctr_score):
            taken_model += 1

    popular_pool = sorted(
        videos_by_id.values(),
        key=lambda v: (-int(v.get("view_count", 0) or 0), str(v.get("video_id", ""))),
    )
    taken_popular = 0
    for video in popular_pool:
        if taken_popular >= n_popular:
            break
        if take(video, "trending", None, None):
            taken_popular += 1

    remaining = [v for v in videos_by_id.values() if str(v.get("video_id", "")) not in seen]
    rng.shuffle(remaining)
    taken_explore = 0
    for video in remaining:
        if taken_explore >= n_explore:
            break
        if take(video, "random", None, None):
            taken_explore += 1

    # 부족분: trending → random 순으로 이어 채움 (태그는 실제 소스)
    for video in popular_pool:
        if len(selected) >= n_total:
            break
        take(video, "trending", None, None)
    leftovers = [v for v in videos_by_id.values() if str(v.get("video_id", "")) not in seen]
    rng.shuffle(leftovers)
    for video in leftovers:
        if len(selected) >= n_total:
            break
        take(video, "random", None, None)

    if skipped_joins:
        logger.warning(
            "model exposure join skipped %d ranked videos absent from trending pool (user_id=%s)",
            skipped_joins,
            user_id,
        )

    rng.shuffle(selected)
    candidates: list[dict] = []
    metadata: dict[tuple[str, str], ExposureMetadata] = {}
    for position, (video, source, model_rank, ctr) in enumerate(selected, start=1):
        video_id = str(video.get("video_id", ""))
        candidates.append(video)
        metadata[(user_id, video_id)] = ExposureMetadata(
            policy="model",
            rank=model_rank if source == "model" else position,
            ctr_score=ctr,
            is_exploration=source == "random",
            policy_version=model_run_id,
            exposure_source=source,  # type: ignore[arg-type]
        )
    return candidates, metadata
```

- [x] **Step 4: 통과 확인**

Run: `uv run python -m pytest tests/test_model_exposure_provider.py -v`
Expected: PASS (Task 2 테스트 전부)

- [x] **Step 5: Commit**

```bash
git add src/pipeline/model_exposure_provider.py tests/test_model_exposure_provider.py
git commit -m "feat: 70/20/10 모델 노출 조립 순수 함수 추가 (#221)"
```

---

### Task 3: BQ 리더 `load_user_rankings`

**Files:**
- Modify: `src/pipeline/model_exposure_provider.py`
- Test: `tests/test_model_exposure_provider.py`

**Interfaces:**
- Produces (Task 4가 사용):
  - `@dataclass(frozen=True, slots=True) class RankingsPartition: by_user: dict[str, list[RankedVideo]]; model_run_id: str | None`
  - `load_user_rankings(client: bigquery.Client, table_id: str, dt: date) -> RankingsPartition` — 0행이면 RuntimeError

- [x] **Step 1: 실패하는 테스트 작성** — 같은 테스트 파일에 추가

```python
import pandas as pd
from datetime import date

from src.pipeline.model_exposure_provider import RankingsPartition, load_user_rankings


class _FakeQueryJob:
    def __init__(self, frame: pd.DataFrame):
        self._frame = frame

    def to_dataframe(self) -> pd.DataFrame:
        return self._frame


class _FakeClient:
    def __init__(self, frame: pd.DataFrame):
        self.frame = frame
        self.queries: list[str] = []

    def query(self, query: str) -> _FakeQueryJob:
        self.queries.append(query)
        return _FakeQueryJob(self.frame)


def _rankings_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "user_id": ["u1", "u1", "u2"],
            "video_id": ["v001", "v002", "v001"],
            "rank": [1, 2, 1],
            "ctr_score": [0.9, 0.8, 0.7],
            "model_run_id": ["run-a"] * 3,
        }
    )


def test_load_user_rankings_groups_by_user_in_rank_order():
    client = _FakeClient(_rankings_frame())
    partition = load_user_rankings(client, "p.d.user_recommendations", date(2026, 7, 22))
    assert [rv.video_id for rv in partition.by_user["u1"]] == ["v001", "v002"]
    assert partition.by_user["u2"][0].rank == 1
    assert partition.model_run_id == "run-a"
    assert "dt = '2026-07-22'" in client.queries[0]


def test_load_user_rankings_fails_fast_on_empty_partition():
    client = _FakeClient(_rankings_frame().iloc[0:0])
    with pytest.raises(RuntimeError, match="2026-07-22"):
        load_user_rankings(client, "p.d.user_recommendations", date(2026, 7, 22))
```

- [x] **Step 2: 실패 확인**

Run: `uv run python -m pytest tests/test_model_exposure_provider.py -v`
Expected: FAIL — `load_user_rankings` ImportError

- [x] **Step 3: 구현** — 같은 모듈에 추가

```python
@dataclass(frozen=True, slots=True)
class RankingsPartition:
    """dt 파티션 1개의 유저별 모델 순위 + 계보."""

    by_user: dict[str, list[RankedVideo]]
    model_run_id: str | None


def load_user_rankings(
    client: bigquery.Client, table_id: str, dt: date
) -> RankingsPartition:
    """user_recommendations의 dt 파티션을 1회 조회해 유저별 순위 맵으로 만든다.

    파티션이 비면 fail-fast — 휴리스틱 대체는 #222의 명시적 플래그로만 한다.
    """
    query = f"""
    SELECT user_id, video_id, rank, ctr_score, model_run_id
    FROM `{table_id}`
    WHERE dt = '{dt.isoformat()}'
    ORDER BY user_id, rank
    """
    frame = client.query(query).to_dataframe()
    if frame.empty:
        raise RuntimeError(
            f"No user_recommendations rows for dt={dt.isoformat()} in {table_id}"
        )

    run_ids = sorted(set(frame["model_run_id"].dropna().astype(str)))
    if len(run_ids) > 1:
        logger.warning("multiple model_run_id in partition, using first: %s", run_ids)
    model_run_id = run_ids[0] if run_ids else None

    by_user: dict[str, list[RankedVideo]] = {}
    for row in frame.itertuples(index=False):
        by_user.setdefault(str(row.user_id), []).append(
            RankedVideo(
                video_id=str(row.video_id),
                rank=int(row.rank),
                ctr_score=None if pd.isna(row.ctr_score) else float(row.ctr_score),
            )
        )
    return RankingsPartition(by_user=by_user, model_run_id=model_run_id)
```

(모듈 상단 import에 `import pandas as pd` 추가.)

- [x] **Step 4: 통과 확인**

Run: `uv run python -m pytest tests/test_model_exposure_provider.py -v`
Expected: PASS

- [x] **Step 5: Commit**

```bash
git add src/pipeline/model_exposure_provider.py tests/test_model_exposure_provider.py
git commit -m "feat: user_recommendations 파티션 리더 추가 (#221)"
```

---

### Task 4: provider 팩토리 `make_model_exposure_provider`

**Files:**
- Modify: `src/pipeline/model_exposure_provider.py`
- Test: `tests/test_model_exposure_provider.py`

**Interfaces:**
- Consumes: Task 2 `build_model_exposures`, Task 3 `RankingsPartition`,
  `CandidateProvider = Callable[[dict, random.Random], list[dict]]` (pipeline.py:123)
- Produces (#222가 사용):
  - `@dataclass(slots=True) class ModelExposureRound: provider: CandidateProvider; metadata: dict[tuple[str, str], ExposureMetadata]; model_run_id: str | None`
  - `make_model_exposure_provider(rankings, videos, *, candidates_per_user=24, personalized_ratio=0.7, popular_ratio=0.2, exploration_ratio=0.1) -> ModelExposureRound`
  - `round.metadata`는 provider 호출이 진행되며 채워진다 —
    `generate_action_log_drafts(..., candidate_provider=round.provider)` 후
    `_expand_events(..., metadata=round.metadata)`로 조인(#222).

- [x] **Step 1: 실패하는 테스트 작성** — 같은 테스트 파일에 추가

```python
from src.pipeline.model_exposure_provider import make_model_exposure_provider


def _partition() -> RankingsPartition:
    return RankingsPartition(
        by_user={"u1": _ranking()}, model_run_id="run-a"
    )


def test_provider_matches_candidate_provider_seam_and_fills_metadata():
    round_ = make_model_exposure_provider(_partition(), _videos())
    candidates = round_.provider({"user_id": "u1"}, random.Random("s:u1"))
    assert len(candidates) == 24
    assert len(round_.metadata) == 24
    assert all(key[0] == "u1" for key in round_.metadata)
    assert round_.model_run_id == "run-a"


def test_provider_returns_trending_random_for_unknown_user():
    round_ = make_model_exposure_provider(_partition(), _videos())
    candidates = round_.provider({"user_id": "u-unknown"}, random.Random("s:u-unknown"))
    assert len(candidates) == 24
    sources = {m.exposure_source for k, m in round_.metadata.items() if k[0] == "u-unknown"}
    assert "model" not in sources


def test_factory_fails_fast_without_videos():
    with pytest.raises(RuntimeError, match="trending"):
        make_model_exposure_provider(_partition(), [])
```

- [x] **Step 2: 실패 확인**

Run: `uv run python -m pytest tests/test_model_exposure_provider.py -v`
Expected: FAIL — `make_model_exposure_provider` ImportError

- [x] **Step 3: 구현** — 같은 모듈에 추가

```python
@dataclass(slots=True)
class ModelExposureRound:
    """provider와 노출 태그 맵의 쌍 — 맵은 provider 호출이 진행되며 채워진다."""

    provider: CandidateProvider
    metadata: dict[tuple[str, str], ExposureMetadata] = field(default_factory=dict)
    model_run_id: str | None = None


def make_model_exposure_provider(
    rankings: RankingsPartition,
    videos: Sequence[dict],
    *,
    candidates_per_user: int = 24,
    personalized_ratio: float = 0.7,
    popular_ratio: float = 0.2,
    exploration_ratio: float = 0.1,
) -> ModelExposureRound:
    """CandidateProvider seam에 주입 가능한 모델 노출 provider를 만든다."""
    if not videos:
        raise RuntimeError("trending videos are required to assemble exposures")

    metadata: dict[tuple[str, str], ExposureMetadata] = {}

    def provider(virtual_user: dict, user_rng: random.Random) -> list[dict]:
        user_id = str(virtual_user.get("user_id", ""))
        candidates, user_meta = build_model_exposures(
            user_id,
            rankings.by_user.get(user_id, []),
            videos,
            user_rng,
            model_run_id=rankings.model_run_id,
            candidates_per_user=candidates_per_user,
            personalized_ratio=personalized_ratio,
            popular_ratio=popular_ratio,
            exploration_ratio=exploration_ratio,
        )
        metadata.update(user_meta)
        return candidates

    return ModelExposureRound(
        provider=provider, metadata=metadata, model_run_id=rankings.model_run_id
    )
```

- [x] **Step 4: 통과 확인**

Run: `uv run python -m pytest tests/test_model_exposure_provider.py -v`
Expected: PASS (파일 전체)

- [x] **Step 5: Commit**

```bash
git add src/pipeline/model_exposure_provider.py tests/test_model_exposure_provider.py
git commit -m "feat: 모델 노출 provider 팩토리 추가 (#221)"
```

---

### Task 5: 최종 검증

- [x] **Step 1: 전체 스위트**

Run: `uv run python -m pytest -q`
Expected: 전부 PASS (기준: main 592 passed + 신규)

- [x] **Step 2: spec 대조** — `docs/specs/2026-07-22-model-exposure-assembly.md`의
  목표 3개·조립 규칙·태그 계약·에러 처리 각 항목이 테스트로 커버되는지 확인.
  spec 서술과 구현이 어긋난 항목은 spec을 갱신(코드가 아니라 문서가 틀린 경우)
  하거나 코드를 수정한다.

- [x] **Step 3: 문서·계획 체크박스 갱신 후 Commit + Push**

```bash
git add docs/plans/2026-07-22-model-exposure-assembly.md
git commit -m "docs: 모델 노출 조립 계획 체크박스 갱신 (#221)"
git push origin feat/221-model-exposure-assembly
```

## Self-Review 결과

- spec 목표 1(BQ 리더)=Task 3, 목표 2(provider)=Task 2·4, 목표 3(태그 계약)=Task 1 — 커버.
- 부족분·조인 실패·결정론·dt fail-fast·하위 호환 각각 테스트 존재 — 커버.
- 미커버(의도): 일일 CLI 배선·fallback 플래그·exposure_source별 집계 리포트 — #222 소관(spec 비범위와 일치).
- 타입 일관성: `RankedVideo`/`RankingsPartition`/`ModelExposureRound`/`ExposureMetadata` 시그니처가 Task 간 동일함을 확인.
