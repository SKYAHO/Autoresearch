# 정책 시뮬레이션 라운드 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 학습된 Reranker가 노출을 선정하는 `model` 정책과 기존 키워드 휴리스틱 `baseline` 정책을 같은 유저·영상 pool에서 나란히 돌려, LLM 판정·합동 CTR 정규화를 거친 event log와 정책 비교 리포트를 산출하는 배치를 만든다.

**Architecture:** `src/pipeline/simulate_policy_round.py` 배치가 `src/serving`의 Reranker를 직접 로드해 점수를 얻고, `autoresearch/action_logs`의 LLM 판정·정규화·이벤트 확장 기계를 재사용한다. 피처 조립은 `build_training_dataset.py`에서 추출한 공용 함수(`src/features/assembly.py`)로 학습과 동일 경로를 쓴다. Spec: `docs/specs/2026-07-20-policy-simulation-round.md`.

**Tech Stack:** Python 3.12, pandas, duckdb, pydantic v2, pyarrow, LightGBM(joblib artifact), MLflow(선택), pytest.

## Global Constraints

- 브랜치: `feat/195-policy-simulation-round` (이슈 #195). 모든 커밋은 이 브랜치에.
- 에이전트 응답·주석·docstring은 한국어 격식체 (CLAUDE.md).
- 모든 새 Python 함수는 타입 힌트(반환 타입 포함) 필수.
- 구조 변경(추출·이동)과 동작 변경(신규 기능)은 커밋을 분리한다.
- 커밋 메시지 끝에 `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` 한 줄.
- 테스트 실행 명령: `uv run python -m pytest tests/<파일>.py -v` (전체: `uv run python -m pytest -v`).
- 의존 방향: `autoresearch/`는 `src/`를 import하지 않는다. `src/`는 `autoresearch/`를 import할 수 있다.
- 시크릿·생성 데이터 파일 커밋 금지.

## 파일 구조 (최종)

```
src/features/assembly.py                 # 신규: 학습·시뮬레이션 공용 피처 조립 (Task 1)
src/pipeline/build_training_dataset.py   # 수정: assembly 함수 사용으로 재배선 (Task 1)
autoresearch/action_logs/schema.py       # 수정: EventLog additive 확장 (Task 2)
autoresearch/action_logs/pipeline.py     # 수정: parquet schema·row 확장(Task 2),
                                         #       provider seam(Task 3), _expand_events 메타(Task 4),
                                         #       normalize_clicks 공개 래퍼(Task 4)
src/pipeline/policy_selector.py          # 신규: Top-K + exploration 선택기 (Task 5)
src/pipeline/simulate_policy_round.py    # 신규: 배치 진입점 + 리포트 (Task 6)
tests/test_features_assembly.py          # 신규 (Task 1)
tests/test_action_logs_schema_policy.py  # 신규 (Task 2)
tests/test_action_logs_pipeline.py       # 수정: seam·메타 테스트 추가 (Task 3, 4)
tests/test_policy_selector.py            # 신규 (Task 5)
tests/test_simulate_policy_round.py      # 신규 (Task 6, 7)
docs/specs/2026-07-20-policy-simulation-round.md  # 수정: manifest→리포트 JSON 문구 (Task 6)
```

---

### Task 1: 피처 조립 공용 함수 추출 (`src/features/assembly.py`) — 구조 변경 전용

`build_training_dataset.main()` 안에 인라인으로 있던 피처 계산(DuckDB SQL 3개 + interaction 계산)을 순수 함수로 추출하고 `main()`을 재배선한다. **동작은 바이트 단위로 동일해야 한다** — 기존 `tests/test_build_training_dataset.py`가 안전망이다.

**Files:**
- Create: `src/features/assembly.py`
- Modify: `src/pipeline/build_training_dataset.py` (91–115행 `derive_preferred_category`, 423–437행 video_feature SQL, 440–454행 user_feature_offline SQL, 457–510행 online_features SQL, 548–586행 interaction 계산부)
- Test: `tests/test_features_assembly.py`

**Interfaces:**
- Consumes: `src/features/feature_builder.py`의 `compute_historical_category_match`, `compute_preferred_category_match`, `compute_topic_similarity`, `embed_keywords` (기존 함수, 시그니처 변경 없음)
- Produces (Task 6이 사용):
  - `compute_video_features(videos_raw: pd.DataFrame, snapshot_date: str) -> pd.DataFrame` — 컬럼: `video_id, category_id, duration_sec, view_count, like_ratio, comment_ratio, days_since_upload`
  - `compute_user_offline_features(personas_raw: pd.DataFrame) -> pd.DataFrame` — 컬럼: `user_id, age_group, occupation`
  - `compute_point_in_time_user_features(event_log: pd.DataFrame, videos_raw: pd.DataFrame, query_points: pd.DataFrame) -> pd.DataFrame` — `query_points`는 `user_id, as_of`(+임의 carry 컬럼). 반환: carry 포함 + `historical_category_affinity, recent_click_count_7d, recent_watch_time_7d, recent_like_count_7d`
  - `compute_interaction_columns(joined: pd.DataFrame) -> pd.DataFrame` — 입력에 `hobbies_and_interests_list, historical_category_affinity, category_id` 필요. `preferred_topics, preferred_category, topic_similarity, historical_category_match, preferred_category_match` 컬럼을 추가해 반환
  - `extract_keywords_safe(text_or_json) -> list[str]`, `derive_preferred_category(keywords) -> list[str]` (build_training_dataset에서 이동, `KEYWORD_TO_CATEGORY` dict 동반 이동)

- [ ] **Step 1: 실패하는 단위 테스트 작성** — `tests/test_features_assembly.py`

```python
"""src/features/assembly.py 공용 피처 조립 함수 단위 테스트."""

import pandas as pd

from src.features.assembly import (
    compute_interaction_columns,
    compute_point_in_time_user_features,
    compute_user_offline_features,
    compute_video_features,
)


def _videos_raw() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "video_id": ["v1", "v2"],
            "categoryId": ["Gaming", "Music"],
            "duration": [120, None],
            "viewCount": [1000, 0],
            "likeCount": [100, 5],
            "commentCount": [10, 1],
            "publishedAt": ["2026-07-01", "2026-07-10"],
        }
    )


def test_compute_video_features_columns_and_values():
    out = compute_video_features(_videos_raw(), "2026-07-11")
    assert list(out.columns) == [
        "video_id", "category_id", "duration_sec", "view_count",
        "like_ratio", "comment_ratio", "days_since_upload",
    ]
    v1 = out[out["video_id"] == "v1"].iloc[0]
    assert v1["duration_sec"] == 120
    assert v1["like_ratio"] == 0.1
    assert v1["days_since_upload"] == 10
    v2 = out[out["video_id"] == "v2"].iloc[0]
    assert v2["duration_sec"] == 300  # COALESCE 기본값
    assert pd.isna(v2["like_ratio"])  # viewCount=0 → NULLIF → NULL


def test_compute_user_offline_features_age_group_buckets():
    personas = pd.DataFrame(
        {"uuid": ["u1", "u2", "u3"], "age": [19, 34, 60], "occupation": ["s", "o", "r"]}
    )
    out = compute_user_offline_features(personas)
    assert list(out.columns) == ["user_id", "age_group", "occupation"]
    assert out["age_group"].tolist() == ["10s", "30s", "50s+"]


def test_compute_point_in_time_user_features_respects_as_of():
    # u1: as_of 이전 클릭 1건(Gaming) → affinity=Gaming, count=1. as_of 이후 이벤트는 무시.
    events = pd.DataFrame(
        {
            "event_id": ["e1", "e2"],
            "user_id": ["u1", "u1"],
            "video_id": ["v1", "v2"],
            "timestamp": ["2026-07-10 10:00:00", "2026-07-12 10:00:00"],
            "clicked": [1, 1],
            "liked": [0, 1],
            "watch_time_sec": [60, 30],
        }
    )
    query_points = pd.DataFrame(
        {"user_id": ["u1"], "as_of": ["2026-07-11 00:00:00"], "tag": ["q1"]}
    )
    out = compute_point_in_time_user_features(events, _videos_raw(), query_points)
    row = out.iloc[0]
    assert row["tag"] == "q1"  # carry 컬럼 보존
    assert row["historical_category_affinity"] == "Gaming"
    assert row["recent_click_count_7d"] == 1
    assert row["recent_watch_time_7d"] == 60
    assert row["recent_like_count_7d"] == 0


def test_compute_interaction_columns_matches():
    joined = pd.DataFrame(
        {
            "hobbies_and_interests_list": ['["gaming"]'],
            "historical_category_affinity": ["Gaming"],
            "category_id": ["Gaming"],
        }
    )
    out = compute_interaction_columns(joined)
    assert out["historical_category_match"].iloc[0] == 1
    assert out["preferred_category_match"].iloc[0] in (0, 1)
    assert 0.0 <= abs(out["topic_similarity"].iloc[0]) <= 1.0
```

- [ ] **Step 2: 실패 확인**

Run: `uv run python -m pytest tests/test_features_assembly.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.features.assembly'`

- [ ] **Step 3: `src/features/assembly.py` 작성**

`build_training_dataset.py`에서 아래를 **그대로 이동/일반화**한다. SQL 본문은 원본과 문자 단위로 동일하게 유지하되, `online_features` SQL만 `FROM event_log_ts e` → `FROM query_points q` 일반화를 적용한다.

```python
"""학습·시뮬레이션 공용 피처 조립 함수.

build_training_dataset.main()의 인라인 DuckDB SQL과 interaction 계산을 추출한
것이다. 학습 데이터셋 생성과 정책 시뮬레이션 라운드(simulate_policy_round)가
같은 코드로 피처를 계산해 학습-서빙 스큐를 방지한다.
"""

import json
from datetime import datetime

import duckdb
import pandas as pd

from src.features.feature_builder import (
    compute_historical_category_match,
    compute_preferred_category_match,
    compute_topic_similarity,
    embed_keywords,
)

# KEYWORD_TO_CATEGORY: build_training_dataset.py 상단의 dict를 그대로 이동한다.
KEYWORD_TO_CATEGORY = {  # (원본 내용 그대로 — 이동 시 복사)
    ...
}


def derive_preferred_category(keywords) -> list:
    """(build_training_dataset.py 91–115행을 그대로 이동 — docstring 포함)"""
    ...


def extract_keywords_safe(text_or_json) -> list:
    """(build_training_dataset.py main() 내부 548–558행 클로저를 모듈 함수로 승격)"""
    if pd.isna(text_or_json):
        return []
    try:
        keywords = json.loads(str(text_or_json))
        if isinstance(keywords, list):
            return [str(k).lower() for k in keywords if k]
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def compute_video_features(videos_raw: pd.DataFrame, snapshot_date: str) -> pd.DataFrame:
    """영상 원본 컬럼(categoryId/duration/viewCount/...)에서 모델 영상 피처를 계산한다."""
    datetime.strptime(snapshot_date, "%Y-%m-%d")  # SQL 보간 전 형식 검증
    con = duckdb.connect()
    con.register("videos_raw", videos_raw)
    return con.execute(
        f"""
        SELECT
            video_id,
            CAST(categoryId AS VARCHAR) AS category_id,
            COALESCE(CAST(duration AS INTEGER), 300) AS duration_sec,
            CAST(viewCount AS BIGINT) AS view_count,
            ROUND(CAST(likeCount AS FLOAT) / NULLIF(CAST(viewCount AS FLOAT), 0), 4) AS like_ratio,
            ROUND(CAST(commentCount AS FLOAT) / NULLIF(CAST(viewCount AS FLOAT), 0), 4) AS comment_ratio,
            DATE_DIFF('day', CAST(publishedAt AS DATE), DATE '{snapshot_date}') AS days_since_upload
        FROM videos_raw
        """
    ).df()


def compute_user_offline_features(personas_raw: pd.DataFrame) -> pd.DataFrame:
    """persona 원본(uuid/age/occupation)에서 오프라인 유저 피처를 계산한다."""
    con = duckdb.connect()
    con.register("personas_raw", personas_raw)
    return con.execute(
        """
        SELECT
            uuid AS user_id,
            CASE
                WHEN age < 20 THEN '10s'
                WHEN age < 30 THEN '20s'
                WHEN age < 40 THEN '30s'
                WHEN age < 50 THEN '40s'
                ELSE '50s+'
            END AS age_group,
            occupation
        FROM personas_raw
        """
    ).df()


def compute_point_in_time_user_features(
    event_log: pd.DataFrame,
    videos_raw: pd.DataFrame,
    query_points: pd.DataFrame,
) -> pd.DataFrame:
    """query_points(user_id, as_of[, carry...])의 각 행에 대해 as_of 직전 기준
    historical_category_affinity와 recent 7일 집계를 계산한다.

    학습 경로는 query_points=노출 이벤트(as_of=impression 시각)로, 시뮬레이션
    경로는 query_points=유저×기준시각 1행으로 호출한다 — 같은 SQL이므로
    point-in-time 계산이 두 경로에서 항상 일치한다.
    """
    con = duckdb.connect()
    con.register("event_log_src", event_log)
    con.register("videos_raw", videos_raw)
    con.register("query_points_src", query_points)
    con.execute("CREATE OR REPLACE TABLE event_log_ts AS SELECT * FROM event_log_src")
    con.execute("CREATE OR REPLACE TABLE query_points AS SELECT * FROM query_points_src")
    carry = [c for c in query_points.columns if c not in ("user_id", "as_of")]
    carry_select = "".join(f'q."{name}",\n            ' for name in carry)
    return con.execute(
        f"""
        SELECT
            q.user_id,
            q.as_of,
            {carry_select}COALESCE(
                (
                    SELECT CAST(v.categoryId AS VARCHAR)
                    FROM event_log_ts AS past
                    JOIN videos_raw AS v ON v.video_id = past.video_id
                    WHERE past.user_id = q.user_id
                      AND CAST(past.timestamp AS TIMESTAMP) < CAST(q.as_of AS TIMESTAMP)
                      AND past.clicked = 1
                    GROUP BY v.categoryId
                    ORDER BY COUNT(*) DESC
                    LIMIT 1
                ),
                'unknown'
            ) AS historical_category_affinity,

            (
                SELECT COUNT(*)
                FROM event_log_ts AS past
                WHERE past.user_id = q.user_id
                  AND past.clicked = 1
                  AND CAST(past.timestamp AS TIMESTAMP) < CAST(q.as_of AS TIMESTAMP)
                  AND CAST(past.timestamp AS TIMESTAMP) >= CAST(q.as_of AS TIMESTAMP) - INTERVAL 7 DAY
            ) AS recent_click_count_7d,

            (
                SELECT COALESCE(SUM(past.watch_time_sec), 0)
                FROM event_log_ts AS past
                WHERE past.user_id = q.user_id
                  AND CAST(past.timestamp AS TIMESTAMP) < CAST(q.as_of AS TIMESTAMP)
                  AND CAST(past.timestamp AS TIMESTAMP) >= CAST(q.as_of AS TIMESTAMP) - INTERVAL 7 DAY
            ) AS recent_watch_time_7d,

            (
                SELECT COUNT(*)
                FROM event_log_ts AS past
                WHERE past.user_id = q.user_id
                  AND past.liked = 1
                  AND CAST(past.timestamp AS TIMESTAMP) < CAST(q.as_of AS TIMESTAMP)
                  AND CAST(past.timestamp AS TIMESTAMP) >= CAST(q.as_of AS TIMESTAMP) - INTERVAL 7 DAY
            ) AS recent_like_count_7d
        FROM query_points q
        """
    ).df()


def compute_interaction_columns(joined: pd.DataFrame) -> pd.DataFrame:
    """preferred/topic/match 상호작용 피처를 계산해 컬럼으로 추가한다.

    입력 필수 컬럼: hobbies_and_interests_list, historical_category_affinity,
    category_id. (build_training_dataset.py Step 2의 계산을 그대로 이동한 것.)
    """
    out = joined.copy()
    out["preferred_topics"] = out["hobbies_and_interests_list"].apply(extract_keywords_safe)
    out["preferred_category"] = out["preferred_topics"].apply(derive_preferred_category)
    out["user_keyword_embeddings"] = out["preferred_topics"].apply(embed_keywords)
    out["topic_similarity"] = out.apply(
        lambda row: compute_topic_similarity(row["user_keyword_embeddings"], row["category_id"]),
        axis=1,
    )
    out["historical_category_match"] = out.apply(
        lambda row: compute_historical_category_match(
            row["historical_category_affinity"], row["category_id"]
        ),
        axis=1,
    )
    out["preferred_category_match"] = out.apply(
        lambda row: compute_preferred_category_match(row["preferred_category"], row["category_id"]),
        axis=1,
    )
    return out
```

주의: `KEYWORD_TO_CATEGORY`와 `derive_preferred_category`는 `build_training_dataset.py`의 원본을 **문자 그대로** 옮긴다(위 코드의 `...` 자리). 이동 후 `build_training_dataset.py`에는 남기지 않고 `from src.features.assembly import derive_preferred_category, extract_keywords_safe`로 재수입한다.

- [ ] **Step 4: 단위 테스트 통과 확인**

Run: `uv run python -m pytest tests/test_features_assembly.py -v`
Expected: PASS (4건)

- [ ] **Step 5: `build_training_dataset.main()` 재배선**

`main()`에서 아래 블록을 함수 호출로 교체한다 (동작 동일):

- 425–437행 `video_feature = con.execute(...)` → `video_feature = compute_video_features(videos, snapshot_date)`
- 440–454행 `user_feature_offline = con.execute(...)` → `user_feature_offline = compute_user_offline_features(personas)`
- 457–510행 `online_features = con.execute(...)` →

```python
    query_points = events.rename(columns={"timestamp": "as_of"})[
        ["user_id", "as_of", "event_id", "video_id", "clicked"]
    ]
    online_features = compute_point_in_time_user_features(events, videos, query_points)
    online_features = online_features.rename(columns={"as_of": "timestamp"})
    online_features["timestamp"] = pd.to_datetime(online_features["timestamp"])
```

- 548–586행 interaction 계산(Step 2 블록) → `joined = compute_interaction_columns(joined)` (기존 print 라인은 계산 뒤에 그대로 유지)
- 91–115행 `derive_preferred_category`와 `KEYWORD_TO_CATEGORY`는 삭제하고 import로 교체

주의: 원본 online_features SQL은 `CAST(e.timestamp AS TIMESTAMP) AS timestamp`로 timestamp 캐스팅을 반환했다. 교체 코드의 `pd.to_datetime` 라인이 그 역할을 대신한다.

- [ ] **Step 6: 전체 기존 테스트 통과 확인 (동일성 안전망)**

Run: `uv run python -m pytest tests/test_build_training_dataset.py tests/test_feature_builder.py tests/test_features_assembly.py -v`
Expected: 전부 PASS. 실패하면 추출이 동작을 바꾼 것이므로 원본과 diff를 재대조한다.

- [ ] **Step 7: 커밋 (구조 변경 전용)**

```bash
git add src/features/assembly.py src/pipeline/build_training_dataset.py tests/test_features_assembly.py
git commit -m "refactor: 학습 피처 조립을 src/features/assembly.py로 추출 (#195)

동작 변경 없음 — 시뮬레이션 라운드가 학습과 동일 코드로 피처를 계산하기
위한 구조 변경. 기존 build_training_dataset 테스트가 동일성을 보증한다.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: EventLog additive 확장 (`schema.py` + parquet/warehouse 직렬화)

**Files:**
- Modify: `autoresearch/action_logs/schema.py:18` (상수), `schema.py:36-77` (EventLog)
- Modify: `autoresearch/action_logs/pipeline.py:142-157` (`EVENT_LOG_PARQUET_SCHEMA`), `pipeline.py:746-767` (`_event_rows`)
- Test: `tests/test_action_logs_schema_policy.py`

**Interfaces:**
- Produces (Task 4, 6이 사용):
  - `SOURCE_ONLINE_SIMULATED: str = "online_simulated"` (schema.py 모듈 상수)
  - `EventLog` 신규 optional 필드: `policy: Literal["baseline", "model"] | None`, `ctr_score: float | None`, `is_exploration: bool | None`, `policy_version: str | None`
  - `EventLog.to_warehouse_row()` dict에 위 4개 키 포함

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_action_logs_schema_policy.py`

```python
"""EventLog 정책 메타데이터 additive 확장·하위 호환 테스트."""

from datetime import UTC, datetime

from autoresearch.action_logs.schema import SOURCE_ONLINE_SIMULATED, EventLog


def _base_kwargs() -> dict:
    return {
        "event_id": "evt_00000000",
        "event_timestamp": datetime(2026, 7, 20, tzinfo=UTC),
        "user_id": "u1",
        "event_type": "impression",
        "video_id": "v1",
    }


def test_historical_event_without_policy_fields_still_validates():
    event = EventLog(**_base_kwargs())  # 기존 historical 로그 형태 그대로
    assert event.policy is None
    assert event.ctr_score is None
    assert event.is_exploration is None
    assert event.policy_version is None


def test_policy_fields_round_trip_to_warehouse_row():
    event = EventLog(
        **_base_kwargs(),
        rank=3,
        source=SOURCE_ONLINE_SIMULATED,
        policy="model",
        ctr_score=0.87,
        is_exploration=False,
        policy_version="run-abc123",
    )
    row = event.to_warehouse_row()
    assert row["source"] == "online_simulated"
    assert row["policy"] == "model"
    assert row["ctr_score"] == 0.87
    assert row["is_exploration"] is False
    assert row["policy_version"] == "run-abc123"


def test_baseline_policy_allows_null_score():
    event = EventLog(**_base_kwargs(), policy="baseline")
    assert event.ctr_score is None
```

- [ ] **Step 2: 실패 확인**

Run: `uv run python -m pytest tests/test_action_logs_schema_policy.py -v`
Expected: FAIL — `ImportError: cannot import name 'SOURCE_ONLINE_SIMULATED'`

- [ ] **Step 3: 구현**

`schema.py:18` `SOURCE_HISTORICAL` 아래에 추가:

```python
SOURCE_ONLINE_SIMULATED = "online_simulated"
```

`EventLog`(schema.py:36)의 `source` 필드 아래에 추가:

```python
    # 정책 시뮬레이션 라운드 메타데이터 (docs/specs/2026-07-20-policy-simulation-round.md).
    # 전부 optional — 기존 historical 로그와 하위 호환.
    policy: Literal["baseline", "model"] | None = None
    ctr_score: float | None = None
    is_exploration: bool | None = None
    policy_version: str | None = None
```

`to_warehouse_row()` 반환 dict 마지막에 추가:

```python
            "policy": self.policy,
            "ctr_score": self.ctr_score,
            "is_exploration": self.is_exploration,
            "policy_version": self.policy_version,
```

`pipeline.py:142` `EVENT_LOG_PARQUET_SCHEMA`의 `generated_at` 필드 앞에 추가:

```python
        pa.field("policy", pa.string()),
        pa.field("ctr_score", pa.float64()),
        pa.field("is_exploration", pa.bool_()),
        pa.field("policy_version", pa.string()),
```

`pipeline.py:746` `_event_rows()`의 row dict에 추가:

```python
                "policy": event.policy,
                "ctr_score": event.ctr_score,
                "is_exploration": event.is_exploration,
                "policy_version": event.policy_version,
```

- [ ] **Step 4: 통과 확인 + 기존 파이프라인 회귀 확인**

Run: `uv run python -m pytest tests/test_action_logs_schema_policy.py tests/test_action_logs_pipeline.py -v`
Expected: 전부 PASS (기존 parquet round-trip 테스트가 새 컬럼과 함께 통과해야 함)

- [ ] **Step 5: 커밋**

```bash
git add autoresearch/action_logs/schema.py autoresearch/action_logs/pipeline.py tests/test_action_logs_schema_policy.py
git commit -m "feat: EventLog에 정책 시뮬레이션 메타데이터 additive 확장 (#195)

policy/ctr_score/is_exploration/policy_version optional 필드 추가.
기존 historical 로그와 하위 호환 (전부 기본 None).

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: 후보 주입 seam (`pipeline.py` candidate provider)

**Files:**
- Modify: `autoresearch/action_logs/pipeline.py:406-438` (`_generate_drafts_isolated`), `pipeline.py:906-963` (`generate_action_log_drafts`)
- Test: `tests/test_action_logs_pipeline.py` (테스트 추가)

**Interfaces:**
- Produces (Task 6이 사용):
  - 타입 별칭 `CandidateProvider = Callable[[dict, random.Random], list[dict]]` (pipeline.py 모듈 수준)
  - `generate_action_log_drafts(..., candidate_provider: CandidateProvider | None = None)` — None이면 기존 `build_candidates` 경로(동작 불변). provider는 `(virtual_user, user_rng)`를 받아 video dict 목록을 반환하며, 빈 목록 반환 시 해당 유저는 기존과 동일하게 건너뛴다.

- [ ] **Step 1: 실패하는 테스트 추가** — `tests/test_action_logs_pipeline.py` 말미에

```python
def test_candidate_provider_overrides_default_selection(tmp_path):
    """candidate_provider 주입 시 build_candidates 대신 주입된 후보만 판정한다."""
    users, videos = _fixture_users(2), build_fixture_video_records(10)
    fixed = [videos[0], videos[1]]  # 항상 같은 2개만 노출

    def provider(virtual_user, user_rng):
        return list(fixed)

    result = generate_action_log_drafts(
        _request(tmp_path), users, videos, RuleBasedActionLogGenerator(),
        candidate_provider=provider,
    )
    judged_pairs = {(d.user_id, d.video_id) for d in result.drafts}
    expected_video_ids = {str(v["video_id"]) for v in fixed}
    assert {pair[1] for pair in judged_pairs} <= expected_video_ids
    assert len(result.drafts) == 2 * len(users)
```

(파일 상단 import에 `generate_action_log_drafts`가 없으면 추가한다. `_fixture_users`, `_request`, `build_fixture_video_records`, `RuleBasedActionLogGenerator`는 이 테스트 파일에 이미 있는 기존 헬퍼를 그대로 쓴다.)

- [ ] **Step 2: 실패 확인**

Run: `uv run python -m pytest tests/test_action_logs_pipeline.py::test_candidate_provider_overrides_default_selection -v`
Expected: FAIL — `TypeError: ... unexpected keyword argument 'candidate_provider'`

- [ ] **Step 3: 구현**

`pipeline.py` 모듈 수준(예: `ActionLogGenerator` Protocol 근처)에 추가:

```python
# 유저별 노출 후보를 외부에서 결정할 때 쓰는 주입 지점. (virtual_user, user_rng)를
# 받아 video dict 목록을 반환한다. None이면 기존 build_candidates 휴리스틱을 쓴다.
CandidateProvider = Callable[[dict, random.Random], list[dict]]
```

`_generate_drafts_isolated` 시그니처에 `candidate_provider: CandidateProvider | None = None` 파라미터를 추가하고(430행 부근):

```python
        user_rng = random.Random(f"{request.seed}:{user_id}")
        if candidate_provider is not None:
            candidates = candidate_provider(virtual_user, user_rng)
        else:
            candidates = build_candidates(
                virtual_user,
                videos,
                request.candidates_per_user,
                request.exploration_ratio,
                user_rng,
                personalized_ratio=request.personalized_ratio,
                popular_ratio=request.popular_ratio,
            )
```

`generate_action_log_drafts` 시그니처에도 동일 파라미터를 추가하고 `_generate_drafts_isolated` 호출에 전달한다.

- [ ] **Step 4: 통과 확인**

Run: `uv run python -m pytest tests/test_action_logs_pipeline.py -v`
Expected: 전부 PASS (기존 테스트 포함 — provider 미주입 경로 동작 불변 확인)

- [ ] **Step 5: 커밋**

```bash
git add autoresearch/action_logs/pipeline.py tests/test_action_logs_pipeline.py
git commit -m "feat: action log 파이프라인에 후보 주입 seam 추가 (#195)

candidate_provider 주입 시 build_candidates 대신 외부 정책이 고른 후보를
판정한다. 미주입 시 동작 불변. src→autoresearch 단방향 의존 유지 장치.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: `_expand_events` 노출 메타데이터·소스·event_id prefix + `normalize_clicks` 공개 래퍼

**Files:**
- Modify: `autoresearch/action_logs/pipeline.py:655-666` (`_clicked_indices` 래퍼 추가), `pipeline.py:668-743` (`_expand_events`, `_emit`)
- Test: `tests/test_action_logs_pipeline.py` (테스트 추가)

**Interfaces:**
- Produces (Task 6이 사용):
  - `@dataclass(frozen=True) class ExposureMetadata: policy: Literal["baseline", "model"]; rank: int; ctr_score: float | None; is_exploration: bool | None; policy_version: str | None` (pipeline.py)
  - `_expand_events(drafts, clicked, request, *, metadata: Mapping[tuple[str, str], ExposureMetadata] | None = None, source: str = SOURCE_HISTORICAL, event_id_prefix: str = "evt")` — metadata 키는 `(user_id, video_id)`. 매칭되는 이벤트(세션 전체 행)에 policy 필드·rank를 태깅한다.
  - `normalize_clicks(drafts: list[ImpressionDraft], target_ctr: float) -> set[int]` — `_clicked_indices`의 공개 래퍼 (합동 정규화를 외부 배치가 쓰기 위함)

- [ ] **Step 1: 실패하는 테스트 추가** — `tests/test_action_logs_pipeline.py` 말미에

```python
def test_expand_events_tags_exposure_metadata_and_prefix():
    from autoresearch.action_logs.pipeline import (
        ExposureMetadata,
        _expand_events,
        normalize_clicks,
    )
    from autoresearch.action_logs.schema import SOURCE_ONLINE_SIMULATED, ImpressionDraft

    drafts = [
        ImpressionDraft(
            user_id="u1", video_id="v1", click_propensity=0.9,
            watch_fraction=0.5, would_like=False, duration_sec=100,
        ),
        ImpressionDraft(
            user_id="u1", video_id="v2", click_propensity=0.1,
            watch_fraction=0.5, would_like=False, duration_sec=100,
        ),
    ]
    clicked = normalize_clicks(drafts, target_ctr=0.5)  # 상위 1건 = v1
    assert clicked == {0}

    metadata = {
        ("u1", "v1"): ExposureMetadata(
            policy="model", rank=1, ctr_score=0.9,
            is_exploration=False, policy_version="run-x",
        ),
        ("u1", "v2"): ExposureMetadata(
            policy="model", rank=2, ctr_score=0.1,
            is_exploration=True, policy_version="run-x",
        ),
    }
    request = EventGenerationRequest(seed=7)
    events = _expand_events(
        drafts, clicked, request,
        metadata=metadata, source=SOURCE_ONLINE_SIMULATED, event_id_prefix="evt_m",
    )
    impressions = [e for e in events if e.event_type == "impression"]
    assert len(impressions) == 2
    assert all(e.source == "online_simulated" for e in events)
    assert all(e.event_id.startswith("evt_m_") for e in events)
    v1_imp = next(e for e in impressions if e.video_id == "v1")
    assert (v1_imp.policy, v1_imp.rank, v1_imp.ctr_score) == ("model", 1, 0.9)
    v1_click = next(e for e in events if e.event_type == "click")
    assert v1_click.policy == "model"  # 세션 행에도 태깅
    v2_imp = next(e for e in impressions if e.video_id == "v2")
    assert v2_imp.is_exploration is True


def test_expand_events_without_metadata_is_unchanged():
    from autoresearch.action_logs.pipeline import _expand_events, normalize_clicks
    from autoresearch.action_logs.schema import ImpressionDraft

    drafts = [
        ImpressionDraft(
            user_id="u1", video_id="v1", click_propensity=0.9,
            watch_fraction=0.5, would_like=False, duration_sec=100,
        ),
    ]
    events = _expand_events(drafts, normalize_clicks(drafts, 0.0), EventGenerationRequest(seed=7))
    assert events[0].event_id == "evt_00000000"
    assert events[0].source == "historical"
    assert events[0].policy is None
```

(`EventGenerationRequest` import가 파일에 없으면 추가한다.)

- [ ] **Step 2: 실패 확인**

Run: `uv run python -m pytest tests/test_action_logs_pipeline.py::test_expand_events_tags_exposure_metadata_and_prefix -v`
Expected: FAIL — `ImportError: cannot import name 'ExposureMetadata'`

- [ ] **Step 3: 구현**

`pipeline.py`의 `_clicked_indices` 아래에 추가:

```python
def normalize_clicks(drafts: list[ImpressionDraft], target_ctr: float) -> set[int]:
    """전역 CTR 정규화의 공개 진입점 — 외부 배치(정책 시뮬레이션)가 합동 pool에
    한 번만 적용할 수 있게 _clicked_indices를 노출한다."""

    return _clicked_indices(drafts, target_ctr)


@dataclass(frozen=True)
class ExposureMetadata:
    """정책 시뮬레이션 노출 1건의 로그 태깅 메타데이터. 키는 (user_id, video_id)."""

    policy: Literal["baseline", "model"]
    rank: int
    ctr_score: float | None
    is_exploration: bool | None
    policy_version: str | None
```

`_expand_events` 시그니처를 다음으로 변경:

```python
def _expand_events(
    drafts: list[ImpressionDraft],
    clicked: set[int],
    request: EventGenerationRequest,
    *,
    metadata: Mapping[tuple[str, str], ExposureMetadata] | None = None,
    source: str = SOURCE_HISTORICAL,
    event_id_prefix: str = "evt",
) -> list[EventLog]:
```

`_emit`을 다음으로 변경 (기존 `rank=None, source=SOURCE_HISTORICAL` 하드코딩 제거):

```python
    def _emit(timestamp, user_id, event_type, video_id, watch=None):
        nonlocal seq
        meta = metadata.get((user_id, video_id)) if metadata else None
        events.append(
            EventLog(
                event_id=f"{event_id_prefix}_{seq:08d}",
                event_timestamp=timestamp,
                user_id=user_id,
                event_type=event_type,
                video_id=video_id,
                watch_time_sec=watch,
                rank=meta.rank if meta else None,
                source=source,
                policy=meta.policy if meta else None,
                ctr_score=meta.ctr_score if meta else None,
                is_exploration=meta.is_exploration if meta else None,
                policy_version=meta.policy_version if meta else None,
            )
        )
        seq += 1
```

import에 `Mapping`(collections.abc), `dataclass`가 없으면 추가. `source` 파라미터 타입은 `str`로 두되 EventLog validator가 Literal을 강제한다.

- [ ] **Step 4: 통과 확인**

Run: `uv run python -m pytest tests/test_action_logs_pipeline.py -v`
Expected: 전부 PASS (기본값 경로 동작 불변 포함)

- [ ] **Step 5: 커밋**

```bash
git add autoresearch/action_logs/pipeline.py tests/test_action_logs_pipeline.py
git commit -m "feat: _expand_events 노출 메타데이터 태깅과 normalize_clicks 공개 (#195)

정책 시뮬레이션이 정책별 이벤트에 policy/rank/ctr_score 등을 태깅하고
합동 정규화를 외부에서 1회 적용할 수 있게 한다. 기본값 경로는 불변.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: 정책 선택기 (`src/pipeline/policy_selector.py`)

**Files:**
- Create: `src/pipeline/policy_selector.py`
- Test: `tests/test_policy_selector.py`

**Interfaces:**
- Consumes: `src/serving/schemas.RerankedVideo` (`video_id: str`, `ctr_score: float`)
- Produces (Task 6이 사용):
  - `@dataclass(frozen=True, slots=True) class Exposure: video_id: str; rank: int; ctr_score: float | None; is_exploration: bool | None`
  - `select_exposures(ranked: list[RerankedVideo], k: int, exploration_ratio: float, rng: random.Random) -> list[Exposure]` — `ranked`는 점수 내림차순(Reranker 출력 그대로). 반환 rank는 반환 목록 순서 1-base.

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_policy_selector.py`

```python
"""select_exposures Top-K + exploration 선택기 단위 테스트."""

import random

import pytest

from src.pipeline.policy_selector import Exposure, select_exposures
from src.serving.schemas import RerankedVideo


def _ranked(n: int) -> list[RerankedVideo]:
    # 점수 내림차순 n개 (v0이 최고점)
    return [RerankedVideo(video_id=f"v{i}", ctr_score=1.0 - i * 0.01) for i in range(n)]


def test_exploitation_takes_top_scores_in_order():
    out = select_exposures(_ranked(20), k=10, exploration_ratio=0.0, rng=random.Random(1))
    assert [e.video_id for e in out] == [f"v{i}" for i in range(10)]
    assert [e.rank for e in out] == list(range(1, 11))
    assert all(e.is_exploration is False for e in out)


def test_exploration_slots_come_from_non_topk():
    out = select_exposures(_ranked(100), k=10, exploration_ratio=0.2, rng=random.Random(1))
    assert len(out) == 10
    explore = [e for e in out if e.is_exploration]
    exploit = [e for e in out if not e.is_exploration]
    assert len(explore) == 2  # round(10 * 0.2)
    assert len(exploit) == 8
    exploit_ids = {e.video_id for e in exploit}
    assert exploit_ids == {f"v{i}" for i in range(8)}
    # exploration은 exploitation 이후 순위·비-Top-K 출신
    assert all(int(e.video_id[1:]) >= 8 for e in explore)
    assert [e.rank for e in out] == list(range(1, 11))


def test_deterministic_given_same_seed():
    a = select_exposures(_ranked(50), k=10, exploration_ratio=0.3, rng=random.Random(7))
    b = select_exposures(_ranked(50), k=10, exploration_ratio=0.3, rng=random.Random(7))
    assert a == b


def test_k_at_least_pool_exposes_everything_without_exploration():
    out = select_exposures(_ranked(5), k=10, exploration_ratio=0.5, rng=random.Random(1))
    assert len(out) == 5
    assert all(e.is_exploration is False for e in out)


def test_invalid_arguments_raise():
    with pytest.raises(ValueError):
        select_exposures(_ranked(5), k=0, exploration_ratio=0.1, rng=random.Random(1))
    with pytest.raises(ValueError):
        select_exposures(_ranked(5), k=3, exploration_ratio=1.5, rng=random.Random(1))
```

- [ ] **Step 2: 실패 확인**

Run: `uv run python -m pytest tests/test_policy_selector.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.pipeline.policy_selector'`

- [ ] **Step 3: 구현** — `src/pipeline/policy_selector.py`

```python
"""model 정책의 노출 선택기 — Reranker 점수 Top-K + exploration 슬롯.

exploration은 폐루프 재학습의 피드백 편향(모델이 좋아하는 것만 노출→학습)을
완화하는 장치다. spec: docs/specs/2026-07-20-policy-simulation-round.md
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from src.serving.schemas import RerankedVideo


@dataclass(frozen=True, slots=True)
class Exposure:
    """정책이 노출하기로 결정한 영상 1건과 로그 태깅용 메타데이터."""

    video_id: str
    rank: int
    ctr_score: float | None
    is_exploration: bool | None


def select_exposures(
    ranked: list[RerankedVideo],
    k: int,
    exploration_ratio: float,
    rng: random.Random,
) -> list[Exposure]:
    """점수 내림차순 ranked에서 exploitation 상위 + 비-Top-K 균등 랜덤 exploration으로
    최대 k개 노출을 뽑는다. rank는 반환 순서 1-base. seed 고정 시 결정론."""
    if k < 1:
        raise ValueError("k must be at least 1")
    if not 0.0 <= exploration_ratio <= 1.0:
        raise ValueError("exploration_ratio must be between 0 and 1")

    n_total = min(k, len(ranked))
    if n_total == len(ranked):
        n_explore = 0  # 전 후보 노출 — exploration이 뽑을 잔여 pool이 없다
    else:
        n_explore = min(round(n_total * exploration_ratio), len(ranked) - n_total)
    n_exploit = n_total - n_explore

    exposures = [
        Exposure(video_id=item.video_id, rank=index + 1, ctr_score=item.ctr_score, is_exploration=False)
        for index, item in enumerate(ranked[:n_exploit])
    ]
    remainder = ranked[n_exploit:]
    for offset, item in enumerate(rng.sample(remainder, n_explore)):
        exposures.append(
            Exposure(
                video_id=item.video_id,
                rank=n_exploit + offset + 1,
                ctr_score=item.ctr_score,
                is_exploration=True,
            )
        )
    return exposures
```

- [ ] **Step 4: 통과 확인**

Run: `uv run python -m pytest tests/test_policy_selector.py -v`
Expected: PASS (5건)

- [ ] **Step 5: 커밋**

```bash
git add src/pipeline/policy_selector.py tests/test_policy_selector.py
git commit -m "feat: 정책 시뮬레이션용 Top-K + exploration 노출 선택기 추가 (#195)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: 배치 진입점 (`src/pipeline/simulate_policy_round.py`) + 리포트 + spec 문구 정정

**Files:**
- Create: `src/pipeline/simulate_policy_round.py`
- Modify: `docs/specs/2026-07-20-policy-simulation-round.md` (컴포넌트 5절 "shard manifest(라운드 메타: `k`, `exploration_ratio`, `policy_version`)를 함께 확장한다" → "라운드 메타(`k`, `exploration_ratio`, `policy_version`)는 리포트 JSON과 `EventLogBatch.request`에 기록한다(shard manifest는 이 배치가 사용하지 않으므로 확장하지 않는다)")
- Test: `tests/test_simulate_policy_round.py`

**Interfaces:**
- Consumes:
  - Task 1: `compute_video_features`, `compute_user_offline_features`, `compute_point_in_time_user_features`, `compute_interaction_columns`
  - Task 3: `generate_action_log_drafts(..., candidate_provider=...)`
  - Task 4: `ExposureMetadata`, `_expand_events(..., metadata=, source=, event_id_prefix=)`, `normalize_clicks`
  - Task 5: `Exposure`, `select_exposures`
  - 기존: `src.serving.model_loader.load_reranker/load_model_settings_from_environment`, `src.serving.service.Reranker`, `src.serving.schemas.CandidateVideo`, `autoresearch.action_logs.candidate.build_candidates`, `write_event_log_parquet`, `write_event_log_warehouse_jsonl`, `write_quarantine_jsonl`, `EventGenerationRequest`, `EventLogBatch`, `SOURCE_ONLINE_SIMULATED`
- Produces:
  - `main(...) -> dict` — 리포트 dict 반환(테스트 용이성). 시그니처는 Step 3 코드 참조. `reranker` 파라미터 주입 시 아티팩트 로드를 건너뛴다(스모크 테스트용, `create_app(reranker=...)`와 같은 패턴).

**핵심 데이터 흐름 (구현 참조용):**

```
personas(uuid,age,occupation,hobbies_and_interests_list) ─┐
virtual_users(user_id, 페르소나 dict)                      ├─ user_id 기준 병합
historical events(wide: user_id,timestamp,clicked,...)    ─┘
videos_raw(video_id,categoryId,duration,viewCount,...,title,description)
  → 유저별: 전체 pool 피처 프레임 → Reranker.rerank_with_diagnostics
  → model 정책: select_exposures / baseline 정책: build_candidates
  → 유저별 합집합 후보 → generate_action_log_drafts(provider 주입)
  → normalize_clicks(합동 1회) → (user,video) clicked 키셋
  → 정책별 _expand_events(metadata, source=online_simulated, prefix 구분)
  → parquet/warehouse/quarantine 저장 + 리포트 JSON/stdout
```

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_simulate_policy_round.py`

합동 정규화 신호 보존(스펙 테스트 3번)과 리포트 계산을 픽스처로 검증한다. LLM은 `RuleBasedActionLogGenerator`, Reranker는 stub을 주입한다.

```python
"""simulate_policy_round 배치 테스트 — stub Reranker + rule-based LLM."""

import random

import numpy as np
import pandas as pd
import pytest

from autoresearch.action_logs.llm_generator import RuleBasedActionLogGenerator
from src.pipeline.simulate_policy_round import build_pool_feature_frame, main
from src.serving.service import Reranker


class _CategoryLovingModel:
    """category_id가 'Gaming'인 후보에 높은 확률을 주는 stub predict_proba."""

    def predict_proba(self, features):
        p1 = np.where(features["category_id"].astype(str) == "Gaming", 0.9, 0.1)
        return np.column_stack([1 - p1, p1])


def _videos_raw(n: int = 30) -> pd.DataFrame:
    rows = []
    for i in range(n):
        cat = "Gaming" if i % 3 == 0 else "Music"
        rows.append(
            {
                "video_id": f"v{i:03d}",
                "categoryId": cat,
                "duration": 100 + i,
                "viewCount": 1000 + i,
                "likeCount": 10,
                "commentCount": 1,
                "publishedAt": "2026-07-01",
                "title": f"{cat} video {i}",
                "description": f"{cat} 설명 {i}",
                "tags": "",
            }
        )
    return pd.DataFrame(rows)


def _personas(n: int = 4) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "uuid": [f"u{i}" for i in range(n)],
            "age": [25] * n,
            "occupation": ["student"] * n,
            "hobbies_and_interests_list": ['["gaming"]'] * n,
        }
    )


def _virtual_users(n: int = 4) -> list[dict]:
    return [
        {
            "user_id": f"u{i}",
            "age": 25,
            "occupation": "student",
            "interest_keywords": ["게임"],
            "hobby_keywords": [],
            "lifestyle_keywords": [],
            "primary_categories": ["Gaming"],
        }
        for i in range(n)
    ]


def _empty_events() -> pd.DataFrame:
    return pd.DataFrame(
        columns=["event_id", "user_id", "video_id", "timestamp", "clicked", "liked", "watch_time_sec"]
    )


@pytest.fixture()
def stub_reranker() -> Reranker:
    feature_columns = (
        "age_group", "occupation", "historical_category_affinity",
        "recent_click_count_7d", "recent_watch_time_7d", "recent_like_count_7d",
        "category_id", "duration_sec", "view_count", "like_ratio",
        "comment_ratio", "days_since_upload", "historical_category_match",
        "preferred_category_match", "topic_similarity",
    )
    return Reranker(
        model=_CategoryLovingModel(),
        feature_columns=feature_columns,
        categorical_categories={"category_id": ("Gaming", "Music")},
    )


def test_build_pool_feature_frame_covers_model_columns(stub_reranker):
    frame = build_pool_feature_frame(
        personas=_personas(1),
        events=_empty_events(),
        videos_raw=_videos_raw(6),
        user_id="u0",
        as_of="2026-07-20 00:00:00",
    )
    assert len(frame) == 6
    for column in stub_reranker.feature_columns:
        assert column in frame.columns, column


def test_round_report_prefers_model_policy_when_model_is_right(tmp_path, stub_reranker):
    """모델이 유저 취향(Gaming)을 맞히면 합동 정규화 후 model CTR ≥ baseline CTR."""
    report = main(
        personas=_personas(),
        virtual_users=_virtual_users(),
        videos_raw=_videos_raw(),
        events=_empty_events(),
        generator=RuleBasedActionLogGenerator(),
        reranker=stub_reranker,
        k=6,
        exploration_ratio=0.0,
        target_ctr=0.2,
        seed=42,
        policy_version="stub-run",
        output_dir=str(tmp_path),
    )
    assert set(report["policies"]) == {"baseline", "model"}
    model = report["policies"]["model"]
    baseline = report["policies"]["baseline"]
    assert model["impressions"] == 6 * 4
    assert 0.0 <= report["overlap_jaccard_mean"] <= 1.0
    # rule-based generator는 관련도 기반 propensity를 주므로 Gaming만 노출한
    # model 정책의 평균 propensity가 baseline(혼합 노출) 이상이어야 한다.
    assert model["mean_click_propensity"] >= baseline["mean_click_propensity"]
    assert (tmp_path / "policy_round_report.json").is_file()
    assert (tmp_path / "event_log.parquet").is_file()


def test_round_events_are_tagged_per_policy(tmp_path, stub_reranker):
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
        target_ctr=0.2,
        seed=42,
        policy_version="stub-run",
        output_dir=str(tmp_path),
    )
    table = pq.read_table(tmp_path / "event_log.parquet").to_pandas()
    assert set(table["policy"].dropna().unique()) == {"baseline", "model"}
    assert (table["source"] == "online_simulated").all()
    model_imps = table[(table["policy"] == "model") & (table["event_type"] == "impression")]
    assert model_imps["ctr_score"].notna().all()
    assert (model_imps["policy_version"] == "stub-run").all()
```

- [ ] **Step 2: 실패 확인**

Run: `uv run python -m pytest tests/test_simulate_policy_round.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.pipeline.simulate_policy_round'`

- [ ] **Step 3: 구현** — `src/pipeline/simulate_policy_round.py`

```python
#!/usr/bin/env python3
"""정책 시뮬레이션 라운드 배치.

baseline(키워드 휴리스틱) vs model(Reranker Top-K) 정책을 같은 유저·영상
pool에서 병행 노출하고, LLM 판정(합집합 1회)·합동 CTR 정규화를 거쳐 정책
태깅된 event log와 비교 리포트를 산출한다.

주의: 두 정책이 같은 (user, video)를 노출하면 동일 판정을 공유하되 이벤트
행은 정책별로 분리 생성된다. 재학습 등 downstream은 반드시 policy 컬럼으로
필터링해야 한다(정책 간 attribution 오염 방지).

spec: docs/specs/2026-07-20-policy-simulation-round.md
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

import pandas as pd

from autoresearch.action_logs.candidate import build_candidates
from autoresearch.action_logs.llm_generator import (
    OpenRouterActionLogGenerator,
    RuleBasedActionLogGenerator,
)
from autoresearch.action_logs.pipeline import (
    ActionLogGenerator,
    ExposureMetadata,
    _expand_events,
    generate_action_log_drafts,
    normalize_clicks,
    write_event_log_parquet,
    write_event_log_warehouse_jsonl,
    write_quarantine_jsonl,
)
from autoresearch.action_logs.schema import (
    ACTION_LOG_SCHEMA_VERSION,
    PROMPT_VERSION,
    SOURCE_ONLINE_SIMULATED,
    EventGenerationRequest,
    EventLog,
    EventLogBatch,
    ImpressionDraft,
)
from src.features.assembly import (
    compute_interaction_columns,
    compute_point_in_time_user_features,
    compute_user_offline_features,
    compute_video_features,
)
from src.pipeline.policy_selector import Exposure, select_exposures
from src.serving.model_loader import (
    load_model_settings_from_environment,
    load_reranker,
)
from src.serving.schemas import CandidateVideo
from src.serving.service import Reranker

BASELINE = "baseline"
MODEL = "model"


def build_pool_feature_frame(
    personas: pd.DataFrame,
    events: pd.DataFrame,
    videos_raw: pd.DataFrame,
    user_id: str,
    as_of: str,
) -> pd.DataFrame:
    """유저 1명 × 전체 영상 pool의 15개 모델 피처 프레임을 학습과 동일 경로로 만든다."""
    video_features = compute_video_features(videos_raw, as_of.split(" ")[0])
    offline = compute_user_offline_features(personas)
    user_offline = offline[offline["user_id"] == user_id]
    if user_offline.empty:
        raise KeyError(f"persona not found for user_id={user_id}")
    query = pd.DataFrame({"user_id": [user_id], "as_of": [as_of]})
    online = compute_point_in_time_user_features(events, videos_raw, query)

    frame = video_features.copy()
    for column in ("age_group", "occupation"):
        frame[column] = user_offline.iloc[0][column]
    for column in (
        "historical_category_affinity",
        "recent_click_count_7d",
        "recent_watch_time_7d",
        "recent_like_count_7d",
    ):
        frame[column] = online.iloc[0][column]
    persona_row = personas[personas["uuid"] == user_id].iloc[0]
    frame["hobbies_and_interests_list"] = persona_row["hobbies_and_interests_list"]
    frame = compute_interaction_columns(frame)
    return frame


def _to_candidate_videos(frame: pd.DataFrame, feature_columns: tuple[str, ...]) -> list[CandidateVideo]:
    """피처 프레임을 Reranker 입력(CandidateVideo 목록)으로 변환한다.

    None/NaN 수치는 float('nan')으로 통일한다(FeatureValue는 None을 허용하지 않는다).
    """
    candidates: list[CandidateVideo] = []
    for _, row in frame.iterrows():
        features = {}
        for column in feature_columns:
            value = row[column]
            if value is None or (isinstance(value, float) and pd.isna(value)):
                value = float("nan")
            elif pd.isna(value):
                value = float("nan")
            features[column] = value
        candidates.append(CandidateVideo(video_id=str(row["video_id"]), features=features))
    return candidates


def main(
    personas: pd.DataFrame,
    virtual_users: list[dict],
    videos_raw: pd.DataFrame,
    events: pd.DataFrame,
    generator: ActionLogGenerator,
    reranker: Reranker | None = None,
    *,
    k: int = 10,
    exploration_ratio: float = 0.1,
    target_ctr: float = 0.02,
    seed: int = 42,
    chunk_size: int = 0,
    max_concurrency: int = 1,
    policy_version: str = "local",
    as_of: str = "2026-07-20 00:00:00",
    output_dir: str = "data/generated/policy_round",
) -> dict:
    """정책 시뮬레이션 라운드를 실행하고 리포트 dict를 반환한다."""
    if reranker is None:
        reranker = load_reranker(load_model_settings_from_environment())  # fail-fast

    video_by_id = {str(v["video_id"]): v for v in videos_raw.to_dict("records")}

    # 1) 유저별 두 정책의 노출 결정 (+ 스코어링 진단 수집)
    exposures_by_user: dict[str, dict[str, list[Exposure]]] = {}
    unseen_counts: dict[str, int] = {}
    skipped_users: list[str] = []
    for index, virtual_user in enumerate(virtual_users):
        user_id = str(virtual_user.get("user_id", f"user_{index}"))
        try:
            frame = build_pool_feature_frame(personas, events, videos_raw, user_id, as_of)
            candidates = _to_candidate_videos(frame, reranker.feature_columns)
            outcome = reranker.rerank_with_diagnostics(candidates)
        except KeyError:
            skipped_users.append(user_id)  # 유저 단위 격리: persona 누락 등
            continue
        for column, values in outcome.unseen_categories.items():
            unseen_counts[column] = unseen_counts.get(column, 0) + len(values)

        model_rng = random.Random(f"{seed}:model:{user_id}")
        model_exposures = select_exposures(outcome.items, k, exploration_ratio, model_rng)

        baseline_rng = random.Random(f"{seed}:{user_id}")  # 기존 pipeline seed 관례와 동일
        baseline_videos = build_candidates(
            virtual_user, list(video_by_id.values()), k, exploration_ratio, baseline_rng
        )
        baseline_exposures = [
            Exposure(video_id=str(v["video_id"]), rank=i + 1, ctr_score=None, is_exploration=None)
            for i, v in enumerate(baseline_videos)
        ]
        exposures_by_user[user_id] = {MODEL: model_exposures, BASELINE: baseline_exposures}

    # 2) 유저별 합집합 후보로 LLM 판정 1회 (provider 주입)
    union_by_user: dict[str, list[dict]] = {}
    for user_id, both in exposures_by_user.items():
        seen: set[str] = set()
        union: list[dict] = []
        for exposure in both[MODEL] + both[BASELINE]:
            if exposure.video_id in seen:
                continue
            seen.add(exposure.video_id)
            union.append(video_by_id[exposure.video_id])
        union_by_user[user_id] = union

    def provider(virtual_user: dict, user_rng: random.Random) -> list[dict]:
        return union_by_user.get(str(virtual_user.get("user_id", "")), [])

    request = EventGenerationRequest(
        target_ctr=target_ctr,
        candidates_per_user=max(1, 2 * k),
        seed=seed,
        chunk_size=chunk_size,
        max_concurrency=max_concurrency,
        output_path=str(Path(output_dir) / "event_log.parquet"),
        warehouse_output_path=str(Path(output_dir) / "event_log.jsonl"),
        quarantine_output_path=str(Path(output_dir) / "event_log_quarantine.jsonl"),
    )
    draft_result = generate_action_log_drafts(
        request, virtual_users, list(video_by_id.values()), generator,
        candidate_provider=provider,
    )
    draft_by_key: dict[tuple[str, str], ImpressionDraft] = {
        (d.user_id, d.video_id): d for d in draft_result.drafts
    }

    # 3) 합동 정규화 1회 → clicked (user, video) 키셋
    clicked_keys = {
        (draft_result.drafts[i].user_id, draft_result.drafts[i].video_id)
        for i in normalize_clicks(draft_result.drafts, target_ctr)
    }

    # 4) 정책별 이벤트 확장 (판정 없는 노출은 quarantine 여파로 제외하고 계수)
    all_events: list[EventLog] = []
    dropped = 0
    per_policy: dict[str, dict[str, float]] = {}
    for policy, prefix, seed_offset in ((BASELINE, "evt_b", 0), (MODEL, "evt_m", 1000)):
        policy_drafts: list[ImpressionDraft] = []
        metadata: dict[tuple[str, str], ExposureMetadata] = {}
        propensities: list[float] = []
        exploration_clicks = 0
        exploration_imps = 0
        for user_id, both in exposures_by_user.items():
            for exposure in both[policy]:
                draft = draft_by_key.get((user_id, exposure.video_id))
                if draft is None:
                    dropped += 1
                    continue
                policy_drafts.append(draft)
                propensities.append(draft.click_propensity)
                metadata[(user_id, exposure.video_id)] = ExposureMetadata(
                    policy=policy,  # type: ignore[arg-type]
                    rank=exposure.rank,
                    ctr_score=exposure.ctr_score,
                    is_exploration=exposure.is_exploration,
                    policy_version=policy_version,
                )
                if exposure.is_exploration:
                    exploration_imps += 1
                    if (user_id, exposure.video_id) in clicked_keys:
                        exploration_clicks += 1
        clicked_indices = {
            i for i, d in enumerate(policy_drafts) if (d.user_id, d.video_id) in clicked_keys
        }
        policy_request = request.model_copy(update={"seed": seed + seed_offset})
        events_out = _expand_events(
            policy_drafts, clicked_indices, policy_request,
            metadata=metadata, source=SOURCE_ONLINE_SIMULATED, event_id_prefix=prefix,
        )
        all_events.extend(events_out)
        impressions = len(policy_drafts)
        clicks = len(clicked_indices)
        per_policy[policy] = {
            "impressions": impressions,
            "clicks": clicks,
            "ctr": round(clicks / impressions, 4) if impressions else 0.0,
            "mean_click_propensity": (
                round(sum(propensities) / len(propensities), 4) if propensities else 0.0
            ),
            "exploration_impressions": exploration_imps,
            "exploration_clicks": exploration_clicks,
        }

    # 5) 노출 겹침률 (유저별 Jaccard 평균)
    jaccards: list[float] = []
    for both in exposures_by_user.values():
        a = {e.video_id for e in both[BASELINE]}
        b = {e.video_id for e in both[MODEL]}
        if a | b:
            jaccards.append(len(a & b) / len(a | b))
    overlap = round(sum(jaccards) / len(jaccards), 4) if jaccards else 0.0

    # 6) 저장 + 리포트
    batch = EventLogBatch(
        schema_version=ACTION_LOG_SCHEMA_VERSION,
        prompt_version=PROMPT_VERSION,
        request=request,
        events=all_events,
    )
    output_path = Path(request.output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    write_event_log_parquet(batch, generator.model_name, output_path)
    write_event_log_warehouse_jsonl(batch, request.warehouse_output_path)
    write_quarantine_jsonl(draft_result.quarantine, request.quarantine_output_path)

    report = {
        "policy_version": policy_version,
        "k": k,
        "exploration_ratio": exploration_ratio,
        "target_ctr": target_ctr,
        "seed": seed,
        "users": len(exposures_by_user),
        "skipped_users": skipped_users,
        "dropped_exposures_without_judgment": dropped,
        "policies": per_policy,
        "overlap_jaccard_mean": overlap,
        "unseen_category_counts": unseen_counts,
        "quarantined_chunks": len(draft_result.quarantine),
    }
    report_path = Path(output_dir) / "policy_round_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return report
```

CLI 어댑터(같은 파일 하단)와 MLflow 기록:

```python
def _cli() -> None:
    """파일 경로 인자를 로드해 main()에 전달하는 CLI 어댑터."""
    parser = argparse.ArgumentParser(description="정책 시뮬레이션 라운드 실행")
    parser.add_argument("--personas", required=True, help="persona csv/parquet 경로")
    parser.add_argument("--virtual-users", required=True, help="virtual user parquet 경로")
    parser.add_argument("--videos", required=True, help="videos_raw csv 경로 (youtube_videos.csv 형식)")
    parser.add_argument("--events", required=True, help="historical wide events csv 경로")
    parser.add_argument("--k", type=int, default=10)
    parser.add_argument("--exploration-ratio", type=float, default=0.1)
    parser.add_argument("--target-ctr", type=float, default=0.02)
    parser.add_argument("--max-users", type=int, default=None)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--chunk-size", type=int, default=0)
    parser.add_argument("--max-concurrency", type=int, default=1)
    parser.add_argument("--policy-version", default="local")
    parser.add_argument("--as-of", default=None, help="기준 시각 (기본: 현재 UTC)")
    parser.add_argument("--output-dir", default="data/generated/policy_round")
    parser.add_argument("--generator", choices=["openrouter", "rule-based"], default="openrouter")
    parser.add_argument("--log-mlflow", action="store_true")
    args = parser.parse_args()

    from datetime import UTC, datetime

    import pyarrow.parquet as pq

    from src.pipeline.build_training_dataset import load_personas

    personas = load_personas(args.personas)
    virtual_users = pq.read_table(args.virtual_users).to_pylist()
    if args.max_users is not None:
        virtual_users = virtual_users[: args.max_users]
    videos_raw = pd.read_csv(args.videos)
    events = pd.read_csv(args.events)
    generator = (
        RuleBasedActionLogGenerator() if args.generator == "rule-based"
        else OpenRouterActionLogGenerator()
    )
    as_of = args.as_of or datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")

    report = main(
        personas=personas,
        virtual_users=virtual_users,
        videos_raw=videos_raw,
        events=events,
        generator=generator,
        k=args.k,
        exploration_ratio=args.exploration_ratio,
        target_ctr=args.target_ctr,
        seed=args.seed,
        chunk_size=args.chunk_size,
        max_concurrency=args.max_concurrency,
        policy_version=args.policy_version,
        as_of=as_of,
        output_dir=args.output_dir,
    )

    if args.log_mlflow:
        import mlflow

        from src.tracking.client import get_or_create_experiment, set_tracking_uri
        from src.tracking.logger import log_metrics, log_parameters

        set_tracking_uri(os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5000"))
        experiment_id = get_or_create_experiment("ctr-model-training")
        with mlflow.start_run(experiment_id=experiment_id, run_name="policy-simulation-round"):
            log_parameters(
                {
                    "round_type": "policy_simulation",
                    "policy_version": report["policy_version"],
                    "k": report["k"],
                    "exploration_ratio": report["exploration_ratio"],
                    "target_ctr": report["target_ctr"],
                    "seed": report["seed"],
                    "users": report["users"],
                }
            )
            log_metrics(
                {
                    "baseline_ctr": report["policies"]["baseline"]["ctr"],
                    "model_ctr": report["policies"]["model"]["ctr"],
                    "baseline_mean_propensity": report["policies"]["baseline"]["mean_click_propensity"],
                    "model_mean_propensity": report["policies"]["model"]["mean_click_propensity"],
                    "overlap_jaccard_mean": report["overlap_jaccard_mean"],
                }
            )


if __name__ == "__main__":
    _cli()
```

(`import os`를 파일 상단 import에 포함한다. `OpenRouterActionLogGenerator()` 생성 인자는 기존 llm_generator의 기본 환경변수 계약을 그대로 따른다 — 필요 인자가 있으면 `autoresearch/action_logs/llm_generator.py:229` 생성자 시그니처를 확인해 맞춘다.)

- [ ] **Step 4: 통과 확인**

Run: `uv run python -m pytest tests/test_simulate_policy_round.py -v`
Expected: PASS (3건). `test_round_report_prefers_model_policy_when_model_is_right`가 실패하면 rule-based generator의 propensity 산식(`llm_generator.py:149`)을 확인해 픽스처의 관련도 격차를 키운다(예: baseline 유저 키워드에서 "게임" 제거).

- [ ] **Step 5: spec 문구 정정**

`docs/specs/2026-07-20-policy-simulation-round.md`의 컴포넌트 5절에서 shard manifest 확장 문구를 위 Files 항목에 적은 문장으로 교체한다 (이 배치는 shard 흐름을 쓰지 않으므로 manifest 확장은 YAGNI).

- [ ] **Step 6: 커밋**

```bash
git add src/pipeline/simulate_policy_round.py tests/test_simulate_policy_round.py docs/specs/2026-07-20-policy-simulation-round.md
git commit -m "feat: 정책 시뮬레이션 라운드 배치와 비교 리포트 추가 (#195)

baseline/model 정책 병행 노출 → 합집합 LLM 판정 → 합동 CTR 정규화 →
정책 태깅 event log + 리포트(JSON/stdout/MLflow 선택) 산출.

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: 엔드투엔드 스모크 + 전체 검증

**Files:**
- Test: `tests/test_simulate_policy_round.py` (스모크 1건 추가)

**Interfaces:**
- Consumes: Task 6의 `main`, 기존 `derive_wide_events` (재학습 호환성 확인)

- [ ] **Step 1: 스모크 테스트 추가** — 산출 로그가 재학습 경로(long→wide)로 소비 가능한지까지 확인

```python
def test_round_output_feeds_retraining_path(tmp_path, stub_reranker):
    """policy=model 필터 후 derive_wide_events가 라벨을 복원할 수 있어야 한다."""
    import pyarrow.parquet as pq

    from src.pipeline.build_training_dataset import derive_wide_events

    main(
        personas=_personas(),
        virtual_users=_virtual_users(),
        videos_raw=_videos_raw(),
        events=_empty_events(),
        generator=RuleBasedActionLogGenerator(),
        reranker=stub_reranker,
        k=6,
        exploration_ratio=0.0,
        target_ctr=0.2,
        seed=42,
        policy_version="stub-run",
        output_dir=str(tmp_path),
    )
    table = pq.read_table(tmp_path / "event_log.parquet").to_pandas()
    model_long = table[table["policy"] == "model"][
        ["event_id", "event_timestamp", "user_id", "event_type", "video_id", "watch_time_sec"]
    ]
    wide = derive_wide_events(model_long)
    impressions = len(model_long[model_long["event_type"] == "impression"])
    assert len(wide) == impressions
    assert wide["clicked"].sum() >= 1  # target_ctr=0.2로 클릭이 존재
```

- [ ] **Step 2: 통과 확인**

Run: `uv run python -m pytest tests/test_simulate_policy_round.py -v`
Expected: PASS (4건)

- [ ] **Step 3: 전체 테스트 스위트**

Run: `uv run python -m pytest -v`
Expected: 전부 PASS. 실패 시 해당 태스크로 돌아가 수정 (특히 스키마 확장이 기존 action_logs 테스트를 깨지 않았는지).

- [ ] **Step 4: 커밋 + push**

```bash
git add tests/test_simulate_policy_round.py
git commit -m "test: 정책 라운드 산출 로그의 재학습 경로 호환 스모크 추가 (#195)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
git push -u origin feat/195-policy-simulation-round
```

---

## Self-Review 결과

- **Spec coverage:** 피처 조립 공용화(Task 1), 정책 선택기(Task 5), 배치 진입점·리포트(Task 6), provider seam(Task 3), 스키마 확장(Task 2), 합동 정규화(Task 4·6), 테스트 5종(Task 1·2·5·6·7), 에러 처리(fail-fast는 Task 6 `load_reranker` 최상단, 유저 격리는 `skipped_users`, LLM chunk 격리는 기존 기계) — 전부 매핑됨. spec의 shard manifest 확장은 Task 6 Step 5에서 리포트 JSON 기록으로 정정.
- **Type consistency:** `select_exposures→list[Exposure]`(Task 5)를 Task 6이 동일 시그니처로 소비. `ExposureMetadata`(Task 4)·`normalize_clicks`(Task 4)·`candidate_provider`(Task 3)의 이름·시그니처가 Task 6 코드와 일치함을 확인.
- **알려진 리스크 (실행자 주의):**
  - Task 1의 `online_features` 일반화는 반환 컬럼 순서가 원본과 다를 수 있다 — downstream은 이름 기반 접근이므로 무해하지만, `tests/test_build_training_dataset.py`가 컬럼 순서를 고정 검증한다면 최종 SELECT(603–627행)가 순서를 결정하므로 영향 없음을 확인할 것.
  - Task 6 테스트의 rule-based propensity 가정이 실제 산식과 다르면 Step 4의 대처를 따를 것.
  - `virtual_users` parquet의 user_id 필드명이 실데이터에서 `virtual_user_id`라면 CLI 어댑터에서 `user_id`로 rename하는 보정이 필요할 수 있다 — 실데이터 라운드 시 확인.
