# action log 당일 슬라이스 통일(A안) 구현 Plan (#295)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `data_lake_action_log` 파티션 시맨틱을 당일 슬라이스로 통일한다 — 소비 SQL을 BETWEEN 프루닝으로 전환하고, event_id를 날짜 네임스페이스로 전역 고유화하고, 트레일링 파티션 4개를 마이그레이션한 뒤 파생 스토어(offline/online)를 정합화한다.

**Architecture:** spec `docs/specs/2026-07-24-action-log-slice-semantics.md`(§1~§7)를 그대로 구현한다. 코드·문서(Task 1~5, PR)가 먼저 머지된 뒤에만 데이터 마이그레이션(Task 6~8)을 실행한다 — 역순이면 머지 전 빌드가 전부 0-스냅샷이 된다.

**Tech Stack:** Python 3.12 + uv, BigQuery(`bq`/`google-cloud-bigquery`), GCS(`gcloud storage`), pyarrow, GKE(kubectl, DNS 엔드포인트), Feast(클러스터 파드 안에서만).

## Global Constraints

- 응답·커밋 메시지·주석·docstring은 **한국어 격식체**. 커밋 형식 `<type>: <설명>` (이 브랜치는 `fix: #295 ...` 권장).
- 브랜치: `fix/295-action-log-slice-semantics` (이슈 #295 연결, 이미 존재).
- 테스트: `uv run --no-sync python -m pytest tests/<파일>::<테스트> -v` (전체는 `uv run python -m pytest`).
- 의존성 변경 금지. `EVENT_LOG_PARQUET_SCHEMA`·`ACTION_LOG_DRAFT_PARQUET_SCHEMA`의 컬럼·타입 변경 금지 (event_id는 string 그대로 — 값 형식만 바뀐다).
- 시크릿·`data/generated/`·생성 parquet 커밋 금지.
- GCP: 프로젝트 `ar-infra-501607`, 버킷 `ar-infra-501607-autoresearch-dev-raw-data`, raw 데이터셋 `data_lake_raw`, feature 데이터셋 `feast_offline_store`. ADC 인증 완료 상태.
- GKE 접근은 반드시 `gcloud container clusters get-credentials autoresearch-dev-gke --region asia-northeast3-a --dns-endpoint`.
- 오래 걸리는 명령(BQ 적재, materialize)은 FOREGROUND로 실행하고 timeout을 넉넉히 잡는다.
- Task 6부터는 **PR 머지 후에만** 실행한다 (Task 5의 게이트).

---

### Task 1: `feature_store_build` dt 술어 BETWEEN 전환

**Files:**
- Modify: `autoresearch/jobs/feature_store_build.py:100` (SQL), `:12-16` 부근 모듈 docstring
- Modify: `docs/guides/data-warehouse.md:383-398` (SSOT SQL 본문·주석)
- Test: `tests/test_feature_store_build.py:122-125`

**Interfaces:**
- Consumes: 없음 (독립 태스크)
- Produces: `_USER_DYNAMIC_SELECT`의 action_log CTE가 `dt BETWEEN P-30 AND P-1` 프루닝을 갖는다. CLI 인자·출력 계약 불변.

- [ ] **Step 1: 회귀 테스트를 BETWEEN 단언으로 교체 (실패 확인용)**

`tests/test_feature_store_build.py:122-125`의 기존 테스트를 다음으로 교체한다:

```python
def test_user_dynamic_snapshot_prunes_action_log_partitions_with_between() -> None:
    # A안(#295): dt=D 파티션은 KST D일 하루치 슬라이스다. 30일 히스토리는
    # dt BETWEEN P-30 AND P-1 프루닝 + timestamp 윈도우로 조립한다.
    sql = _incremental_sql(feature_store_build.USER_DYNAMIC_FEATURE)

    assert "AND dt = DATE '2026-07-21'" not in sql
    assert (
        "AND dt BETWEEN DATE_SUB(DATE '2026-07-21', INTERVAL 30 DAY)" in sql
    )
    assert "AND DATE_SUB(DATE '2026-07-21', INTERVAL 1 DAY)" in sql
```

- [ ] **Step 2: 실패 확인**

Run: `uv run --no-sync python -m pytest tests/test_feature_store_build.py::test_user_dynamic_snapshot_prunes_action_log_partitions_with_between -v`
Expected: FAIL (`AND dt = DATE '2026-07-21'` 가 아직 존재)

- [ ] **Step 3: SQL 교체**

`autoresearch/jobs/feature_store_build.py:100`의

```sql
    AND dt = DATE '{partition_date}'
```

를 다음으로 교체한다 (timestamp 윈도우 2줄은 그대로 둔다):

```sql
    AND dt BETWEEN DATE_SUB(DATE '{partition_date}', INTERVAL 30 DAY)
               AND DATE_SUB(DATE '{partition_date}', INTERVAL 1 DAY)
```

같은 파일 모듈 docstring의 "raw action log는 ``dt``가 ``--partition-date``와 같은 단일 파티션만 소비한다. 각 일일 파티션이 독립적인 30일 히스토리이므로 여러 ``dt``를 합치면 중복 집계가 발생한다." 문장을 다음으로 교체한다:

```text
raw action log는 당일 슬라이스 파티션(dt=D = KST D일 하루치)을
``dt BETWEEN P-30 AND P-1``로 프루닝해 30일 히스토리를 조립한다(#295 A안).
슬라이스는 서로소이므로 이 합산은 중복이 아니다.
```

- [ ] **Step 4: 테스트 통과 확인 + 인접 테스트 회귀 확인**

Run: `uv run --no-sync python -m pytest tests/test_feature_store_build.py -v`
Expected: 전부 PASS (특히 `test_user_dynamic_incremental_sql_builds_a_single_snapshot`의 `INTERVAL 30 DAY` count 단언은 BETWEEN 추가로 3이 되어도 `>= 2`라 통과)

- [ ] **Step 5: SSOT 문서 동기화**

`docs/guides/data-warehouse.md`의 action_log CTE(±385~398행)에서:
- 주석 `-- 각 dt는 독립적인 30일 히스토리이므로 대상 파티션 하나만 읽는다.` → `-- dt=D는 KST D일 하루치 슬라이스다(#295 A안). 30일 히스토리는 BETWEEN 프루닝으로 조립한다.`
- `AND dt = DATE '{partition_date}'` → Step 3과 동일한 BETWEEN 2줄.

- [ ] **Step 6: dry-run으로 실 SQL 검증**

Run: `uv run --no-sync python -m autoresearch.jobs.feature_store_build --project ar-infra-501607 --partition-date 2026-07-21 --tables user_dynamic_feature --dry-run`
Expected: `"status": "succeeded"` (BigQuery 파서 통과)

- [ ] **Step 7: Commit**

```bash
git add autoresearch/jobs/feature_store_build.py docs/guides/data-warehouse.md tests/test_feature_store_build.py
git commit -m "fix: #295 feature build dt 술어를 슬라이스 BETWEEN 프루닝으로 전환"
```

---

### Task 2: event_id 날짜 네임스페이스 전역 고유화

**Files:**
- Modify: `autoresearch/action_logs/pipeline.py:17` (import), `:768-797` (`_expand_events`/`_emit`)
- Test: `tests/test_action_logs_pipeline.py:984` (기존 단언 교체) + 신규 테스트 1개

**Interfaces:**
- Consumes: 없음
- Produces: 모든 생성 경로(daily merge `daily.py:1429`, `expand_action_log_drafts`)의 event_id가 `{prefix}_{YYYYMMDD}_{seq:08d}` 형식이 된다 (YYYYMMDD = 해당 이벤트 timestamp의 KST 날짜). 소비 측(`build_training_dataset`의 조인)은 event_id를 불투명 문자열로만 쓰므로 무변경.

- [ ] **Step 1: 신규 실패 테스트 작성**

`tests/test_action_logs_pipeline.py`의 `test_expand_events_without_metadata_is_unchanged` 아래에 추가한다:

```python
def test_expand_events_event_ids_are_date_namespaced_and_unique():
    # #295 A안: event_id = {prefix}_{이벤트 KST 날짜}_{seq}. 파티션(dt=KST 날짜)
    # 네임스페이스가 들어가므로 파티션 간 충돌이 구조적으로 불가능해진다.
    import re
    from datetime import datetime, timedelta, timezone

    from autoresearch.action_logs.pipeline import _expand_events, select_clicks_per_slate
    from autoresearch.action_logs.schema import EventGenerationRequest, ImpressionDraft

    kst = timezone(timedelta(hours=9))
    drafts = [
        ImpressionDraft(
            user_id=f"u{i}", video_id=f"v{i}", click_propensity=0.1,
            watch_fraction=0.5, would_like=False, duration_sec=100,
        )
        for i in range(3)
    ]
    request = EventGenerationRequest(
        click_threshold=0.55, seed=7,
        history_end=datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc),
    )
    events = _expand_events(drafts, select_clicks_per_slate(drafts, 1.0), request)

    assert len(events) == 3
    ids = [e.event_id for e in events]
    assert len(set(ids)) == len(ids)
    for event in events:
        match = re.fullmatch(r"evt_(\d{8})_(\d{8})", event.event_id)
        assert match, event.event_id
        expected_day = event.event_timestamp.astimezone(kst).strftime("%Y%m%d")
        assert match.group(1) == expected_day
```

- [ ] **Step 2: 실패 확인**

Run: `uv run --no-sync python -m pytest tests/test_action_logs_pipeline.py::test_expand_events_event_ids_are_date_namespaced_and_unique -v`
Expected: FAIL (`re.fullmatch` None — 현재 형식은 `evt_00000000`)

- [ ] **Step 3: 구현**

`autoresearch/action_logs/pipeline.py:17`의 import를 확장한다:

```python
from datetime import UTC, timedelta, timezone
```

모듈 상수부(예: `_MAX_DURATION` 근처)에 추가한다:

```python
# 이벤트 KST 날짜가 event_id 네임스페이스다(#295 A안: dt 파티션 = KST 당일 슬라이스).
_KST = timezone(timedelta(hours=9))
```

`_emit`(±797행)의 event_id 생성을 교체한다:

```python
event_id=f"{event_id_prefix}_{timestamp.astimezone(_KST):%Y%m%d}_{seq:08d}",
```

- [ ] **Step 4: 기존 형식 단언 갱신**

`tests/test_action_logs_pipeline.py:984`의

```python
    assert events[0].event_id == "evt_00000000"
```

를 다음으로 교체한다:

```python
    assert re.fullmatch(r"evt_\d{8}_00000000", events[0].event_id)
```

(파일 상단에 `import re`가 없으면 추가한다. `:961`의 `startswith("evt_m_")` 단언은 새 형식에서도 그대로 통과하므로 두 곳 외 변경 없음.)

- [ ] **Step 5: 전체 액션로그 테스트 통과 확인**

Run: `uv run --no-sync python -m pytest tests/test_action_logs_pipeline.py tests/test_action_logs_daily.py tests/test_build_training_dataset.py -v`
Expected: 전부 PASS (daily merge 결정성 테스트 `:407`은 형식 무관 비교라 통과해야 한다 — 실패하면 구현이 비결정적이란 뜻이므로 여기서 멈추고 원인을 본다)

- [ ] **Step 6: Commit**

```bash
git add autoresearch/action_logs/pipeline.py tests/test_action_logs_pipeline.py
git commit -m "fix: #295 event_id에 이벤트 KST 날짜 네임스페이스를 넣어 전역 고유화"
```

---

### Task 3: 소급 event_id 재작성 스크립트

**Files:**
- Create: `scripts/rewrite_action_log_event_ids.py`
- Test: `tests/test_rewrite_action_log_event_ids.py`

**Interfaces:**
- Consumes: 없음 (Task 2의 형식 규칙만 공유)
- Produces: `rewrite_event_ids(table: pa.Table, partition_date: datetime.date) -> pa.Table` + CLI `python scripts/rewrite_action_log_event_ids.py --input <파일.parquet> --partition-date YYYY-MM-DD --output <파일.parquet>`. Task 6의 마이그레이션이 이 CLI를 사용한다.

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_rewrite_action_log_event_ids.py`를 생성한다:

```python
"""scripts/rewrite_action_log_event_ids.py의 재작성 규칙 테스트."""
from datetime import date

import pyarrow as pa
import pytest

from scripts.rewrite_action_log_event_ids import rewrite_event_ids


def _table(event_ids: list[str]) -> pa.Table:
    return pa.table({
        "event_id": pa.array(event_ids, pa.string()),
        "user_id": pa.array(["u"] * len(event_ids), pa.string()),
    })


def test_rewrites_legacy_ids_with_partition_date_namespace() -> None:
    table = rewrite_event_ids(_table(["evt_00000000", "evt_00001234"]), date(2026, 7, 18))
    assert table.column("event_id").to_pylist() == [
        "evt_20260718_00000000", "evt_20260718_00001234",
    ]
    # 다른 컬럼은 보존된다.
    assert table.column("user_id").to_pylist() == ["u", "u"]


def test_already_namespaced_ids_are_unchanged_idempotent() -> None:
    ids = ["evt_20260718_00000000", "evt_m_20260713_00000007"]
    table = rewrite_event_ids(_table(ids), date(2026, 7, 18))
    assert table.column("event_id").to_pylist() == ids


def test_unrecognized_id_format_fails_loudly() -> None:
    with pytest.raises(ValueError, match="event_id"):
        rewrite_event_ids(_table(["weird-id"]), date(2026, 7, 18))
```

- [ ] **Step 2: 실패 확인**

Run: `uv run --no-sync python -m pytest tests/test_rewrite_action_log_event_ids.py -v`
Expected: FAIL (`ModuleNotFoundError: scripts.rewrite_action_log_event_ids`)

- [ ] **Step 3: 스크립트 구현**

`scripts/rewrite_action_log_event_ids.py`를 생성한다:

```python
"""action log parquet의 event_id를 날짜 네임스페이스 형식으로 소급 재작성한다.

[파이프라인] 데이터 레이크 마이그레이션 구간(#295 A안) — GCS에서 내려받은
파티션 parquet 하나를 입력으로 받아 event_id만 재작성한 parquet을 출력한다.
업로드·BQ 재적재는 담당하지 않는다(runbook의 gcloud/bq 절차가 담당).

[기능] 레거시 ``{prefix}_{seq:08d}`` event_id를 파티션 날짜 네임스페이스
``{prefix}_{YYYYMMDD}_{seq:08d}``로 바꾼다. 이미 새 형식인 id는 그대로 두므로
재실행이 멱등하다. 인식할 수 없는 형식은 조용히 통과시키지 않고 실패한다.
"""
from __future__ import annotations

import argparse
import re
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

_LEGACY = re.compile(r"^(?P<prefix>.+?)_(?P<seq>\d{8})$")
_NAMESPACED = re.compile(r"^.+_\d{8}_\d{8}$")


def rewrite_event_ids(table: pa.Table, partition_date: date) -> pa.Table:
    """레거시 event_id에 파티션 날짜 네임스페이스를 주입한 새 Table을 돌려준다."""
    day = partition_date.strftime("%Y%m%d")
    rewritten: list[str] = []
    for event_id in table.column("event_id").to_pylist():
        if event_id is not None and _NAMESPACED.match(event_id):
            rewritten.append(event_id)
            continue
        match = _LEGACY.match(event_id or "")
        if not match:
            raise ValueError(f"인식할 수 없는 event_id 형식: {event_id!r}")
        rewritten.append(f"{match.group('prefix')}_{day}_{match.group('seq')}")
    index = table.column_names.index("event_id")
    return table.set_column(index, "event_id", pa.array(rewritten, pa.string()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--partition-date", required=True, type=date.fromisoformat)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)

    table = pq.read_table(args.input)
    result = rewrite_event_ids(table, args.partition_date)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(result, args.output)
    print(f"[완료] {args.input} -> {args.output} ({result.num_rows} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 통과 확인**

Run: `uv run --no-sync python -m pytest tests/test_rewrite_action_log_event_ids.py -v`
Expected: 3개 전부 PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/rewrite_action_log_event_ids.py tests/test_rewrite_action_log_event_ids.py
git commit -m "feat: #295 event_id 소급 재작성 마이그레이션 스크립트 추가"
```

---

### Task 4: 문서·runbook의 파티션 계약 정정

**Files:**
- Modify: `docs/specs/2026-07-22-feature-store-build-batch.md:55` 부근
- Modify: `docs/runbooks/2026-07-23-action-log-feature-loop.md` (불변 조건 + 업로드 규약)

**Interfaces:**
- Consumes: Task 1의 새 SQL 계약 문구
- Produces: 저장소 문서가 슬라이스 계약 단일 서술을 갖는다

- [ ] **Step 1: #283 spec 정정**

`docs/specs/2026-07-22-feature-store-build-batch.md`의 "`data_lake_action_log`의 각 `dt` 파티션은 독립적인 30일 히스토리 스냅샷이다." 문단 바로 위에 정정 블록을 추가한다:

```markdown
> **[정정 2026-07-24, #295]** 아래 "독립적인 30일 히스토리" 계약은 A안(당일
> 슬라이스 통일)으로 대체되었다. 현행 계약은
> `docs/specs/2026-07-24-action-log-slice-semantics.md`를 따른다: dt=D는 KST
> D일 하루치 슬라이스이고, 빌드는 `dt BETWEEN P-30 AND P-1`로 조립한다.
```

- [ ] **Step 2: runbook 정정**

`docs/runbooks/2026-07-23-action-log-feature-loop.md`의 불변 조건 항목
"action log의 일일 `dt` 파티션 하나는 독립적인 30일 히스토리다. 소비자는 검증 대상 `dt` 하나만 선택하며 파티션 간 UNION을 하지 않는다."
를 다음으로 교체한다:

```markdown
- action log의 `dt=D` 파티션은 KST D일 하루치 슬라이스다(#295 A안). 이벤트는
  실제 발생 당일 timestamp 그대로 `dt=이벤트 당일`에 업로드한다 — "D-1 이벤트
  → dt=D 트레일링" 관행과 30일 합성 확장 업로드는 폐지되었다. 소비자는
  `dt BETWEEN P-30 AND P-1`(+timestamp 윈도우)로 히스토리를 조립하며, 이
  조립의 전제는 event_id 날짜 네임스페이스(`evt_{YYYYMMDD}_{seq}`)다.
- 업로드 전 대상 `dt`가 비어 있는지 `gcloud storage ls`로 확인한다
  (`load_raw_to_bigquery`는 WRITE_TRUNCATE 전체 재적재).
```

- [ ] **Step 3: 전체 테스트 + Commit**

Run: `uv run python -m pytest` → 전부 PASS 확인 후:

```bash
git add docs/specs/2026-07-22-feature-store-build-batch.md docs/runbooks/2026-07-23-action-log-feature-loop.md
git commit -m "docs: #295 파티션 계약 서술을 당일 슬라이스로 정정"
```

---

### Task 5: PR 생성 + 머지 게이트

**Files:** 없음 (프로세스 태스크)

- [ ] **Step 1: 최종 검증**

```bash
uv run python -m pytest -v
git diff --check
```
Expected: 전부 PASS, whitespace 오류 없음

- [ ] **Step 2: PR 생성**

```bash
git push
gh pr create --repo SKYAHO/Autoresearch \
  --title "fix: #295 action log 파티션 시맨틱을 당일 슬라이스로 통일(A안)" \
  --body "$(cat <<'EOF'
Part of #295 (마이그레이션 완료 후 이슈를 닫습니다 — Closes 아님)

## 변경
- `feature_store_build` action_log dt 술어: `dt = P` → `dt BETWEEN P-30 AND P-1` (슬라이스 프루닝, CLI 인터페이스 불변 → Airflow DAG 무변경)
- event_id 생성: `evt_{seq}` → `{prefix}_{이벤트 KST 날짜 YYYYMMDD}_{seq}` (전역 고유화, 실측 176,646건 충돌 해소 전제)
- 소급 재작성 스크립트 `scripts/rewrite_action_log_event_ids.py` (멱등)
- 문서·runbook 계약 서술 정정

## 머지 후 절차 (plan Task 6~8)
GCS 트레일링 파티션 4개 처분 → 슬라이스 event_id 재작성 → BQ 재적재 → offline 스냅샷 재빌드 → online materialize. **머지 전 마이그레이션 금지** (BETWEEN 빌드 부재 시 0-스냅샷).

Spec: `docs/specs/2026-07-24-action-log-slice-semantics.md`
Plan: `docs/plans/2026-07-24-action-log-slice-semantics.md`

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: 리뷰 대응 후 머지 (게이트)**

리뷰 승인(1명) 후: `git rebase origin/main && git push --force-with-lease` 필요 시 수행(브랜치 보호의 up-to-date 요구), squash merge. **머지가 확인되기 전에는 Task 6으로 진행하지 않는다.**

```bash
gh pr merge --repo SKYAHO/Autoresearch --squash
git switch main && git pull
```

---

### Task 6: GCS 마이그레이션 + BQ 재적재 (머지 후, 운영)

**Files:** 없음 (운영 태스크 — 로컬 작업 디렉터리 `data/generated/migration-295/`, gitignore 영역)

**Interfaces:**
- Consumes: Task 3의 CLI, 머지된 main
- Produces: GCS 최종 파티션 = 슬라이스 11개(event_id 재작성) + dt=2026-07-23(라운드 N 재라벨, 256행). BQ `data_lake_action_log` 재적재 완료(기대 총 1,872,677행), event_id 전역 충돌 0.

- [ ] **Step 1: 사전 스냅샷 기록**

```bash
BUCKET=ar-infra-501607-autoresearch-dev-raw-data
gcloud storage ls gs://$BUCKET/data_lake/action_log/
bq query --use_legacy_sql=false --format=csv \
  'SELECT dt, COUNT(*) n, COUNTIF(event_type="click") clicks FROM `ar-infra-501607.data_lake_raw.data_lake_action_log` GROUP BY dt ORDER BY dt' \
  | tee data/generated/migration-295/before.csv
```
Expected: 파티션 15개 (슬라이스 11: 07-07, 07-12~21 + 트레일링 4: 07-23~26). 다르면 멈추고 원인 확인.

- [ ] **Step 2: 전체 로컬 백업**

```bash
mkdir -p data/generated/migration-295/backup
gcloud storage cp -r gs://$BUCKET/data_lake/action_log/ data/generated/migration-295/backup/
```

- [ ] **Step 3: 라운드 N 재라벨 준비 (dt=07-24 → dt=07-23)**

라운드 N(dt=2026-07-24)은 실측상 전 행이 KST 07-23 하루치다(spec §5 표). 재작성 후 새 dt로 준비한다:

```bash
uv run --no-sync python scripts/rewrite_action_log_event_ids.py \
  --input  "data/generated/migration-295/backup/action_log/dt=2026-07-24/part-0.parquet" \
  --partition-date 2026-07-23 \
  --output "data/generated/migration-295/relabel/dt=2026-07-23/part-0.parquet"
```
Expected: `[완료] ... (256 rows)`

- [ ] **Step 4: 트레일링 4개 아카이브 이동**

```bash
for d in 2026-07-23 2026-07-24 2026-07-25 2026-07-26; do
  gcloud storage mv "gs://$BUCKET/data_lake/action_log/dt=$d" \
    "gs://$BUCKET/archive/action_log_trailing/dt=$d"
done
gcloud storage ls gs://$BUCKET/data_lake/action_log/   # 슬라이스 11개만 남았는지
gcloud storage ls gs://$BUCKET/archive/action_log_trailing/  # 4개 이동됐는지
```

- [ ] **Step 5: 재라벨본 업로드**

```bash
gcloud storage ls "gs://$BUCKET/data_lake/action_log/dt=2026-07-23/" 2>&1  # 비어 있어야 함(에러 = 정상)
gcloud storage cp "data/generated/migration-295/relabel/dt=2026-07-23/part-0.parquet" \
  "gs://$BUCKET/data_lake/action_log/dt=2026-07-23/part-0.parquet"
```

- [ ] **Step 6: 슬라이스 11개 event_id 재작성 (down → rewrite → up)**

```bash
for d in 2026-07-07 2026-07-12 2026-07-13 2026-07-14 2026-07-15 2026-07-16 \
         2026-07-17 2026-07-18 2026-07-19 2026-07-20 2026-07-21; do
  uv run --no-sync python scripts/rewrite_action_log_event_ids.py \
    --input  "data/generated/migration-295/backup/action_log/dt=$d/part-0.parquet" \
    --partition-date "$d" \
    --output "data/generated/migration-295/rewritten/dt=$d/part-0.parquet"
  gcloud storage cp "data/generated/migration-295/rewritten/dt=$d/part-0.parquet" \
    "gs://$BUCKET/data_lake/action_log/dt=$d/part-0.parquet"
done
```
주의: 백업 폴더의 실제 파일명이 `part-0.parquet`이 아니면(`ls`로 확인) 그 이름을 쓴다. 행 수가 백업과 같은지 각 `[완료]` 출력으로 확인한다.

- [ ] **Step 7: BQ 재적재 + 검산**

```bash
uv run --no-sync python scripts/load_raw_to_bigquery.py \
  --project ar-infra-501607 --dataset data_lake_raw \
  --bucket $BUCKET --tables action_log
```
Expected: `[OK] 1872677 rows` (= 1,877,674 − round_a 174 − 라운드N 256 − R0 2,510 − R1 2,313 + 재라벨 256)

```bash
bq query --use_legacy_sql=false --format=csv \
  'SELECT dt, COUNT(*) n, COUNTIF(event_type="click") clicks FROM `ar-infra-501607.data_lake_raw.data_lake_action_log` GROUP BY dt ORDER BY dt'
```
Expected: 슬라이스 11개는 `before.csv`와 행수·클릭수 동일, dt=2026-07-23은 `256,6`, 07-24~26 없음.

```bash
bq query --use_legacy_sql=false --format=csv \
  'SELECT COUNT(*) colliding FROM (SELECT event_id FROM `ar-infra-501607.data_lake_raw.data_lake_action_log` GROUP BY event_id HAVING COUNT(*) > 1)'
```
Expected: `colliding` = **0** (마이그레이션 전 176,646 → 0)

---

### Task 7: offline 스냅샷 재빌드 + online materialize (운영)

**Files:** 없음 (운영 태스크)

**Interfaces:**
- Consumes: Task 6 완료 상태의 BQ
- Produces: `feast_offline_store.user_dynamic_feature`의 snap 07-22Z~07-25Z가 슬라이스 계약 기준으로 재계산되고, 신규 snap 07-26Z(P=2026-07-27)가 Redis에 반영된다.

- [ ] **Step 1: 스냅샷 재빌드 (기존 트레일링 기준 4개 + 최신 1개)**

```bash
for p in 2026-07-23 2026-07-24 2026-07-25 2026-07-26 2026-07-27; do
  uv run --no-sync python -m autoresearch.jobs.feature_store_build \
    --project ar-infra-501607 --partition-date "$p" --tables user_dynamic_feature
done
```
Expected: 5회 모두 `"status": "succeeded"`

- [ ] **Step 2: 스냅샷 검산 (snapshot vs raw 재계산 일치)**

각 P(5개)에 대해 실행하고 expected == actual 확인:

```bash
P=2026-07-24  # 5개 날짜 반복
bq query --use_legacy_sql=false --format=csv "
WITH raw AS (
  SELECT COUNT(*) c FROM \`ar-infra-501607.data_lake_raw.data_lake_action_log\`
  WHERE event_type='click'
    AND dt BETWEEN DATE_SUB(DATE '$P', INTERVAL 30 DAY) AND DATE_SUB(DATE '$P', INTERVAL 1 DAY)
    AND event_timestamp >= TIMESTAMP_SUB(TIMESTAMP(DATE '$P','Asia/Seoul'), INTERVAL 7 DAY)
    AND event_timestamp < TIMESTAMP(DATE '$P','Asia/Seoul')
), snap AS (
  SELECT SUM(recent_click_count_7d) c FROM \`ar-infra-501607.feast_offline_store.user_dynamic_feature\`
  WHERE event_timestamp = TIMESTAMP(DATE '$P','Asia/Seoul')
)
SELECT raw.c AS expected, snap.c AS actual FROM raw, snap"
```
주의: 값이 이전 트레일링 기준과 크게 달라지는 것(예: P=07-24가 6 → 슬라이스 5일치 합산 수만 건)이 **정상**이다 — 시맨틱이 바뀌었기 때문이며, 위 쿼리의 자기일관성(expected==actual)만이 판정 기준이다.

- [ ] **Step 3: 클러스터 materialize (일회성 파드)**

```bash
gcloud container clusters get-credentials autoresearch-dev-gke --region asia-northeast3-a --dns-endpoint
```

`autoresearch-serving-looptest` deployment의 image·env·serviceAccountName(`autoresearch-app`)을 복제한 일회성 Pod로 실행한다 (2026-07-24 세션에서 검증된 매니페스트 패턴 — `kubectl get deploy autoresearch-serving-looptest -n autoresearch -o json`으로 image digest·env를 그대로 복사, command만 교체):

```
python -m autoresearch.jobs.feast_materialize --views UserDynamicView \
  --start-ts 2026-07-22T00:00:00+00:00 --end-ts 2026-07-27T00:00:00+00:00
```
(end-ts는 최신 snap 07-26 15:00Z보다 미래여야 한다.)

```bash
kubectl wait --for=jsonpath='{.status.phase}'=Succeeded pod/<파드명> -n autoresearch --timeout=420s
kubectl logs <파드명> -n autoresearch | tail -5   # "status": "succeeded" 확인
kubectl delete pod <파드명> -n autoresearch
```

- [ ] **Step 4: online 값 검증 (일회성 파드)**

같은 스펙의 파드에서 아래 Python을 실행한다. **주의: `FeatureStore(repo_path=...)` 직접 생성은 Redis CA 검증 실패 — 반드시 bootstrap 경유**:

```python
from feature_repo import bootstrap
bootstrap.ensure_redis_ca_bundle()
store = bootstrap.load_feature_store("feature_repo")
print(store.get_online_features(
    features=["UserDynamicView:recent_click_count_7d",
              "UserDynamicView:recent_view_count_7d",
              "UserDynamicView:total_event_count_7d"],
    entity_rows=[{"user_id": u} for u in ["vu_0028", "vu_3359", "vu_0328"]],
).to_dict())
```

기대값은 offline에서 미리 조회해 대조한다:

```bash
bq query --use_legacy_sql=false --format=csv \
  'SELECT user_id, recent_click_count_7d, recent_view_count_7d, total_event_count_7d
   FROM `ar-infra-501607.feast_offline_store.user_dynamic_feature`
   WHERE event_timestamp = TIMESTAMP("2026-07-26 15:00:00") AND user_id IN ("vu_0028","vu_3359","vu_0328")'
```
Expected: online == offline snap 07-26 15:00Z (최신). 검증 후 파드 삭제, `kubectl get pods -n autoresearch`로 잔여 파드 없음 확인.

---

### Task 8: 이슈 마감

**Files:** 없음 (프로세스 태스크)

- [ ] **Step 1: #295 완료 증거 코멘트 + close**

Task 6~7의 실측(재적재 행수, 충돌 0 쿼리 결과, 스냅샷 검산 표, online 대조)을 코멘트로 남기고 spec 완료 조건 체크리스트를 갱신한 뒤 close한다.

- [ ] **Step 2: #286 재범위 코멘트**

spec §3 그대로: BETWEEN은 A안에서 정합한 API가 되었으므로 #286의 남은 범위는 ① `events_start/end_date` dt·timestamp 이중 역할 해소(인자 의미를 "이벤트 발생 KST 날짜 범위"로 문서화·정리), ② 필요 시 파티션 시대 가드 제거·정리임을 코멘트로 기록한다.

- [ ] **Step 3: 메모리·로컬 정리**

`data/generated/migration-295/` 백업은 보존(커밋 금지). 세션 메모리의 파티션 계약 노트를 "통일 완료" 상태로 갱신한다.
