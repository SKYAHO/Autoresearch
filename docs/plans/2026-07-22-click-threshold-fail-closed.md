# click_threshold fail-closed + 캘리브레이션 헬퍼 구현 계획 (#260)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development(권장) 또는 superpowers:executing-plans로 이 계획을 Task 단위로 구현한다. 각 Step은 체크박스(`- [ ]`)로 추적하고, 각 Task는 TDD, 최종 통합은 verification-before-completion을 적용한다.

**Goal:** `click_threshold`를 fail-closed(미지정 시 실패)로 바꾸고, 목표 CTR에 맞는 커트라인을 산출하는 분석 헬퍼(순수 함수 + CLI)를 추가한다.

**Architecture:** 순수 함수 `recommend_click_threshold`를 먼저 TDD로 만들고(포맷-무관, 향후 (C) 재사용 이음매), draft parquet를 읽는 얇은 CLI로 감싼다. 별도로 `click_threshold` 기본값 8곳을 제거해 required로 전환한다.

**Tech Stack:** Python 3.12, Pydantic v2, pyarrow, pytest, uv.

## Global Constraints

- 브랜치는 이슈 #260의 `feat/260-click-threshold-fail-closed`를 사용한다.
- per-slate 클릭 선정 로직(`select_clicks_per_slate`), 노출 조립, LLM 프롬프트/응답, watch_time/like 파생은 **변경하지 않는다.**
- 순수 함수는 LLM·모델·파일 IO 없이 데이터만 받는다(향후 (C) 재사용 이음매 유지).
- fail-closed는 request/manifest 필드 Pydantic required + CLI `required=True`로 일관 적용한다.
- 선행: #255(PR #259). 설계: `docs/specs/2026-07-22-click-threshold-fail-closed.md`

---

## 파일 구조

| 파일 | 책임 | 변경 |
| --- | --- | --- |
| `autoresearch/action_logs/calibration.py` | 커트라인 추천 순수 함수 | 신규 |
| `autoresearch/jobs/click_threshold_calibrate.py` | draft parquet → 추천 CLI | 신규 |
| `autoresearch/action_logs/schema.py` | request/manifest 계약 | `click_threshold` default 제거(required) |
| `autoresearch/jobs/action_log.py` | 공개 CLI | `--click-threshold required=True` |
| `autoresearch/action_logs/daily.py` | 일일 스레딩 | `click_threshold` 기본값 제거 |
| `src/pipeline/simulate_policy_round.py` | 정책 시뮬 | 기본값 제거 + CLI required |
| `scripts/generate_action_logs_scale.py` | 스케일 스크립트 | CLI required |
| `docs/guides/action-log.md` | 운영 가이드 | 캘리브레이션 절차 + fail-closed 계약 + 합동 pool 노트 |
| 각 test 파일 | required 반영 + 신규 | 아래 Task별 |

---

## Task 1: 커트라인 추천 순수 함수

**Files:**
- Create: `autoresearch/action_logs/calibration.py`
- Test: `tests/test_click_threshold_calibration.py`

**Interfaces:**
- Produces: `ThresholdRecommendation`, `recommend_click_threshold(per_user_max_propensity: Sequence[float], impressions: int, target_ctr: float) -> ThresholdRecommendation`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_click_threshold_calibration.py`:

```python
import pytest

from autoresearch.action_logs.calibration import recommend_click_threshold


def test_recommends_threshold_hitting_target_ctr() -> None:
    # 10 유저, 100 노출, target 3% → n_click=3 → 3번째 큰 값(0.7)이 커트라인.
    per_user_max = [0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2, 0.1, 0.05]
    rec = recommend_click_threshold(per_user_max, impressions=100, target_ctr=0.03)
    assert rec.recommended_threshold == 0.7
    assert rec.achieved_ctr == 0.03
    assert rec.users == 10
    assert rec.impressions == 100


def test_sweep_ctr_is_monotone_non_increasing() -> None:
    per_user_max = [0.9, 0.7, 0.5, 0.3, 0.1]
    rec = recommend_click_threshold(per_user_max, impressions=100, target_ctr=0.02)
    ctrs = [ctr for _, ctr in rec.sweep]
    assert ctrs == sorted(ctrs, reverse=True)


def test_errors_when_target_exceeds_ceiling() -> None:
    # ceiling = users/impressions = 10/100 = 0.1
    with pytest.raises(ValueError):
        recommend_click_threshold([0.5] * 10, impressions=100, target_ctr=0.2)


def test_errors_on_zero_target() -> None:
    with pytest.raises(ValueError):
        recommend_click_threshold([0.5] * 10, impressions=100, target_ctr=0.0)


def test_errors_on_empty_input() -> None:
    with pytest.raises(ValueError):
        recommend_click_threshold([], impressions=0, target_ctr=0.02)
```

- [ ] **Step 2: 실패 확인**

Run: `uv run --no-sync python -m pytest tests/test_click_threshold_calibration.py -v`
Expected: FAIL (모듈 없음)

- [ ] **Step 3: 구현**

`autoresearch/action_logs/calibration.py`:

```python
"""click_threshold 캘리브레이션 분석 — 목표 CTR에 맞는 커트라인 산출."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ThresholdRecommendation:
    """목표 CTR을 달성하는 커트라인과 진단."""

    recommended_threshold: float
    achieved_ctr: float
    target_ctr: float
    users: int
    impressions: int
    sweep: tuple[tuple[float, float], ...]
    per_user_max_quantiles: Mapping[str, float]


def _quantile(sorted_desc: list[float], q: float) -> float:
    # sorted_desc: 내림차순. q 분위(0~1)에 해당하는 값(가장 가까운 순위).
    if not sorted_desc:
        return 0.0
    asc = sorted_desc[::-1]
    idx = min(len(asc) - 1, max(0, round(q * (len(asc) - 1))))
    return asc[idx]


def recommend_click_threshold(
    per_user_max_propensity: Sequence[float],
    impressions: int,
    target_ctr: float,
) -> ThresholdRecommendation:
    """유저별 최고 click_propensity 분포에서 목표 CTR에 맞는 커트라인을 추천한다.

    per-slate 최대 1클릭이므로 CTR(t) = (max>=t 인 유저 수) / impressions.
    목표 CTR을 위해 n_click=round(target_ctr*impressions)명이 클릭해야 하므로,
    유저별 최고값 내림차순의 n_click번째 값을 커트라인으로 추천한다.
    """
    values = sorted(per_user_max_propensity, reverse=True)
    users = len(values)
    if users == 0 or impressions <= 0:
        raise ValueError("per_user_max_propensity must be non-empty and impressions > 0")
    ceiling = users / impressions
    if not 0.0 < target_ctr <= ceiling:
        raise ValueError(f"target_ctr must be in (0, {ceiling:.4f}] (CTR ceiling)")

    n_click = min(users, max(1, round(target_ctr * impressions)))
    threshold = values[n_click - 1]
    clickers = sum(1 for v in values if v >= threshold)
    achieved = round(clickers / impressions, 4)

    candidates = sorted(set(values), reverse=True)
    sweep = tuple(
        (round(t, 4), round(sum(1 for v in values if v >= t) / impressions, 4))
        for t in candidates
    )
    quantiles = {
        f"p{int(q * 100)}": round(_quantile(values, q), 4)
        for q in (0.5, 0.75, 0.9, 0.95, 0.99)
    }
    return ThresholdRecommendation(
        recommended_threshold=threshold,
        achieved_ctr=achieved,
        target_ctr=target_ctr,
        users=users,
        impressions=impressions,
        sweep=sweep,
        per_user_max_quantiles=quantiles,
    )
```

- [ ] **Step 4: 통과 확인**

Run: `uv run --no-sync python -m pytest tests/test_click_threshold_calibration.py -v`
Expected: PASS (5개)

- [ ] **Step 5: 커밋**

```bash
git add autoresearch/action_logs/calibration.py tests/test_click_threshold_calibration.py
git commit -m "feat: #260 커트라인 추천 순수 함수 추가"
```

---

## Task 2: 캘리브레이션 CLI (draft parquet → 추천 JSON)

**Files:**
- Create: `autoresearch/jobs/click_threshold_calibrate.py`
- Test: `tests/test_click_threshold_calibrate_job.py`

**Interfaces:**
- Consumes: Task 1의 `recommend_click_threshold`; `read_action_log_draft_parquet`, `ImpressionDraft`
- Produces: `main(argv: Sequence[str] | None = None) -> int` (공개 batch 종료 코드), `--draft-path`, `--target-ctr`

- [ ] **Step 1: 실패 테스트 작성**

`tests/test_click_threshold_calibrate_job.py`: draft parquet fixture를 쓰고 CLI가 추천 JSON을 emit하는지, 인자 누락 시 실패하는지 확인한다.

```python
import json

from autoresearch.action_logs.pipeline import write_action_log_draft_parquet
from autoresearch.action_logs.schema import ImpressionDraft
from autoresearch.jobs.click_threshold_calibrate import main


def _draft(user_id: str, video_id: str, cp: float) -> ImpressionDraft:
    return ImpressionDraft(
        user_id=user_id, video_id=video_id, click_propensity=cp,
        watch_fraction=0.5, would_like=False, duration_sec=100,
    )


def test_cli_emits_recommendation(tmp_path, capsys) -> None:
    drafts = [
        _draft("u1", "a", 0.9), _draft("u1", "b", 0.2),
        _draft("u2", "c", 0.3), _draft("u2", "d", 0.1),
    ]
    path = tmp_path / "drafts.parquet"
    write_action_log_draft_parquet(drafts, path)
    code = main(["--draft-path", str(path), "--target-ctr", "0.25"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["impressions"] == 4
    assert payload["users"] == 2
    assert "recommended_threshold" in payload


def test_cli_requires_target_ctr(tmp_path) -> None:
    import pytest
    with pytest.raises(SystemExit):
        main(["--draft-path", str(tmp_path / "x.parquet")])
```

시그니처(확인됨): `write_action_log_draft_parquet(drafts: list[ImpressionDraft], output_path, *, filesystem=None)`, `read_action_log_draft_parquet(input_path, *, filesystem=None) -> list[ImpressionDraft]`. `--target-ctr` 없이 호출하면 argparse가 `SystemExit`.

- [ ] **Step 2: 실패 확인**

Run: `uv run --no-sync python -m pytest tests/test_click_threshold_calibrate_job.py -v`
Expected: FAIL

- [ ] **Step 3: 구현**

`autoresearch/jobs/click_threshold_calibrate.py` (기존 `jobs/action_log_quality.py`의 argparse·emit·종료코드 패턴을 따른다):

```python
"""draft parquet에서 목표 CTR에 맞는 click_threshold를 추천하는 공개 batch 명령."""

from __future__ import annotations

import argparse
import json
import logging
from collections import defaultdict
from typing import Sequence

from autoresearch.action_logs.calibration import recommend_click_threshold
from autoresearch.action_logs.pipeline import read_action_log_draft_parquet

logger = logging.getLogger(__name__)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--draft-path", required=True)
    parser.add_argument("--target-ctr", type=float, required=True)
    return parser


def _per_user_max(drafts) -> tuple[list[float], int]:
    best: dict[str, float] = defaultdict(float)
    impressions = 0
    for d in drafts:
        impressions += 1
        if d.click_propensity > best[d.user_id]:
            best[d.user_id] = d.click_propensity
    return list(best.values()), impressions


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    drafts = read_action_log_draft_parquet(args.draft_path)
    per_user_max, impressions = _per_user_max(drafts)
    try:
        rec = recommend_click_threshold(per_user_max, impressions, args.target_ctr)
    except ValueError as exc:
        logger.error("calibration failed: %s", exc)
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(
        json.dumps(
            {
                "status": "succeeded",
                "recommended_threshold": rec.recommended_threshold,
                "achieved_ctr": rec.achieved_ctr,
                "target_ctr": rec.target_ctr,
                "users": rec.users,
                "impressions": rec.impressions,
                "per_user_max_quantiles": dict(rec.per_user_max_quantiles),
                "sweep": [list(row) for row in rec.sweep],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 통과 확인**

Run: `uv run --no-sync python -m pytest tests/test_click_threshold_calibrate_job.py -v`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add autoresearch/jobs/click_threshold_calibrate.py tests/test_click_threshold_calibrate_job.py
git commit -m "feat: #260 캘리브레이션 CLI 추가"
```

---

## Task 3: fail-closed — click_threshold 기본값 제거

**Files:**
- Modify: `autoresearch/action_logs/schema.py:126,171`
- Modify: `autoresearch/jobs/action_log.py:133`
- Modify: `autoresearch/action_logs/daily.py:824,976`
- Modify: `src/pipeline/simulate_policy_round.py:138,326`
- Modify: `scripts/generate_action_logs_scale.py:49`
- Modify: 영향받는 모든 test (아래 grep)

**Interfaces:**
- Produces: `EventGenerationRequest.click_threshold`(required), `ActionLogShardManifest.click_threshold`(required), CLI `--click-threshold`(required)

- [ ] **Step 1: fail-closed negative 테스트 작성**

`tests/test_action_logs_pipeline.py`(또는 schema 테스트)에 추가:

```python
import pytest
from pydantic import ValidationError

from autoresearch.action_logs.schema import EventGenerationRequest


def test_event_generation_request_requires_click_threshold() -> None:
    with pytest.raises(ValidationError):
        EventGenerationRequest()  # click_threshold 미지정 → 실패
```

`tests/test_action_logs_daily.py`에 CLI required 확인:

```python
def test_cli_requires_click_threshold() -> None:
    from autoresearch.jobs.action_log import _build_parser
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["--mode", "single", "--partition-date", "2026-07-22"])
```

- [ ] **Step 2: 실패 확인**

Run: `uv run --no-sync python -m pytest tests/test_action_logs_pipeline.py -k requires_click_threshold tests/test_action_logs_daily.py -k requires_click_threshold -v`
Expected: FAIL (아직 기본값 있어 통과 안 함)

- [ ] **Step 3: 기본값 제거**

- `schema.py:171` `click_threshold: float = 0.55` → `click_threshold: float`
- `schema.py:126` `click_threshold: float = Field(default=0.55, ge=0.0, le=1.0)` → `click_threshold: float = Field(ge=0.0, le=1.0)`
- `jobs/action_log.py:133` `--click-threshold ... default=0.55` → `... required=True` (default 인자 제거)
- `daily.py:824,976` `click_threshold: float = 0.55` → `click_threshold: float`
- `simulate_policy_round.py:138` `click_threshold: float = 0.55` → `click_threshold: float`
- `simulate_policy_round.py:326` `--click-threshold ... default=0.55` → `... required=True`
- `scripts/generate_action_logs_scale.py:49` `--click-threshold ... default=0.55` → `... required=True`

- [ ] **Step 4: 모든 기존 생성부에 명시값 주입**

Run: `grep -rn "EventGenerationRequest(" tests/ src/ autoresearch/ | grep -v "click_threshold"`
각 매치가 `click_threshold`를 넘기지 않으면 적절한 값(예: `click_threshold=0.5`)을 추가한다. 마찬가지로 `run_daily_action_log*`, `simulate main(`, draft 생성 호출부 중 기본값에 의존하던 곳을 명시로 바꾼다. 목표: fail-closed 후에도 기존 동작 테스트가 명시값으로 통과.

- [ ] **Step 5: 통과 확인**

Run: `uv run --no-sync python -m pytest tests/test_action_logs_pipeline.py tests/test_action_logs_daily.py tests/test_simulate_policy_round.py -v`
Expected: PASS (negative 포함).

- [ ] **Step 6: 커밋**

```bash
git add autoresearch/action_logs/schema.py autoresearch/jobs/action_log.py autoresearch/action_logs/daily.py src/pipeline/simulate_policy_round.py scripts/generate_action_logs_scale.py tests/
git commit -m "feat: #260 click_threshold 기본값 제거로 fail-closed 전환"
```

---

## Task 4: 문서 — 캘리브레이션 절차 + fail-closed 계약

**Files:**
- Modify: `docs/guides/action-log.md`

**Interfaces:** 없음(문서)

- [ ] **Step 1: 캘리브레이션·fail-closed 문서화**

`docs/guides/action-log.md`에 절을 추가한다:

- **캘리브레이션 절차:** 기본(champion) 모델로 폐루프 1회 실행 → draft parquet 확보 → `python -m autoresearch.jobs.click_threshold_calibrate --draft-path <path> --target-ctr 0.015` 로 분포·추천 산출 → 추천 커트라인을 확정 → 그 값을 `--click-threshold`로 운영 실행.
- **fail-closed 계약:** `click_threshold`에 기본값이 없으므로 미지정 시 실패한다. 캘리브레이션 전 운영 실행 금지.
- **(부수) 합동 pool 클릭 의미론:** 정책 시뮬은 두 정책 노출의 합집합에 per-slate 선정 1회 → 유저별 최고 1건이 승자. 승자 영상이 두 정책에 겹치면 공유 판정되어 유저합 최대 2, per-(policy,user)는 1 보장.

`docs/archive/**`·PR 리포트 HTML은 손대지 않는다.

- [ ] **Step 2: 커밋**

```bash
git add docs/guides/action-log.md
git commit -m "docs: #260 캘리브레이션 절차와 fail-closed 계약 문서화"
```

---

## Task 5: 전체 회귀 검증

**Files:** 없음(발견한 결함은 소유 Task로 돌아가 실패 테스트부터 추가)

- [ ] **Step 1: dev 전체 테스트**

Run: `uv sync --frozen && uv run --no-sync python -m pytest -q`
Expected: 전체 PASS.

- [ ] **Step 2: 정적 검증**

```bash
uv run --no-sync ruff check autoresearch tests tools
uv lock --check
git diff --check
```

Expected: 통과.

- [ ] **Step 3: fail-closed·CLI 스모크**

```bash
uv run --no-sync python -m autoresearch.jobs.click_threshold_calibrate --help
grep -rn "click_threshold.*= 0.55\|default=0.55" autoresearch/ src/ scripts/ || echo "→ 기본값 잔존 없음"
```

Expected: `--click-threshold` 없이 action_log 실행이 실패, 기본값 0.55 잔존 0.

## 완료 기준

- `click_threshold` 미지정 시 request/manifest/CLI 모두 명확히 실패한다.
- 구버전 형태 manifest(click_threshold 부재)가 역직렬화에서 실패한다.
- `click_threshold_calibrate`가 draft parquet에서 목표 CTR 대응 커트라인을 산출하고, 순수 함수는 LLM·IO 없이 테스트로 고정된다.
- 캘리브레이션 절차와 실행 전 필수 전제가 문서에 명문화된다.
- per-slate 클릭 선정·노출 조립·LLM 프롬프트는 불변이다.

## 롤백

기본값 재부여와 헬퍼 삭제로 되돌린다. per-slate 클릭 계약(#255)은 불변이라 데이터 롤백 없음.
