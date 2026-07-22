# 정책 라운드 draft 덤프·리플레이 (캘리브레이션 폐루프)

> 작성: 2026-07-23 | 상태: 설계(리뷰 대기) | 이슈: #267
> 선행: #255(슬레이트당 최대 1클릭), #260(click_threshold fail-closed·캘리브레이션 헬퍼)

## 목표

`click_threshold` 캘리브레이션을 오프라인 폐루프에서 **실제로 돌릴 수 있게**
만든다.

- **AS-IS:** `autoresearch.jobs.click_threshold_calibrate`는 draft parquet을
  입력으로 받지만, `simulate_policy_round`는 LLM 판정 결과(`ImpressionDraft`)를
  메모리에서 쓰고 버린다. 캘리브레이션 입력이 **존재하지 않는다.**
- **TO-BE:** 판정을 parquet + 사이드카 메타로 남기고(`덤프`), 그 판정을 다시
  읽어 **커트라인만 바꿔 재실행**할 수 있게 한다(`리플레이`).

## 배경 — 왜 리플레이까지 필요한가

`recommend_click_threshold`는 **유저별 최고 `click_propensity` 분포**에서
커트라인을 뽑는다(`action_logs/calibration.py:31`). 이 커트라인은 **그 분포에
대해서만** 목표 CTR을 보장한다.

LLM 판정은 비결정적이므로, 추천 커트라인으로 시뮬레이션을 재실행하면
propensity가 새로 뽑히고 목표 CTR이 재현되지 않는다. 게다가 실행마다 LLM
비용이 다시 발생한다. 따라서 "판정(비싸고 비결정적)"과 "커트라인 적용(싸고
결정적)"을 분리해야 캘리브레이션이 검증 가능해진다.

이 분리는 코드상 이미 존재한다 — `generate_action_log_drafts()`(LLM)와
`select_clicks_per_slate()` → `_expand_events()`(커트라인)가 별개 단계이고,
`daily.py`의 merge 경로는 이미 저장된 draft를 판정 없이 재확장한다. 이 spec은
그 경계를 정책 라운드 배치에도 노출하는 것이다.

## 현재 구조 (AS-IS)

`src/pipeline/simulate_policy_round.py`:

1. 유저별로 baseline·model 두 정책의 노출을 결정한다(리랭커는 결정적, 노출
   선택 RNG는 `seed`로 고정 → **재현 가능**).
2. 두 정책 노출의 **합집합**을 후보로 LLM 판정 1회 →
   `draft_result.drafts` (`simulate_policy_round.py:206`).
3. `select_clicks_per_slate(draft_result.drafts, click_threshold)`로 합집합
   슬레이트당 최대 1클릭을 선정 → `clicked_keys`.
4. 정책별로 이벤트를 확장하고, 판정이 없는 노출은 **조용히 건너뛰며**
   `dropped_exposures_without_judgment`만 증가시킨다
   (`simulate_policy_round.py:233`).
5. `event_log.parquet` / `.jsonl` / quarantine / 리포트(JSON·HTML)를 쓴다.
   `drafts`는 저장하지 않는다.

이벤트 로그의 계보는 `write_event_log_parquet(batch, generator.model_name, ...)`
로 기록되므로(`action_logs/pipeline.py:967`), 판정을 재사용하는 실행에서도
**원본 LLM 모델명**이 필요하다.

## 설계

### 1. 덤프 — 판정 저장

`main()`이 LLM 판정 직후 출력 디렉터리에 두 파일을 추가로 쓴다.

**`<output-dir>/action_log_drafts.parquet`**
기존 `write_action_log_draft_parquet(draft_result.drafts, path)`를 그대로
호출한다. `ACTION_LOG_DRAFT_PARQUET_SCHEMA`는 `daily.py`의 shard/merge가 함께
쓰는 **공유 계약**이므로 컬럼을 추가하지 않는다.

**`<output-dir>/action_log_drafts_meta.json`** (사이드카)

```json
{
  "llm_model": "<generator.model_name>",
  "prompt_version": "<PROMPT_VERSION>",
  "schema_version": "<ACTION_LOG_SCHEMA_VERSION>",
  "exposure_args": {
    "seed": 42,
    "k": 10,
    "exploration_ratio": 0.1,
    "as_of": "2026-07-23 00:00:00"
  },
  "policy_version": "local",
  "users": 8,
  "drafts": 118,
  "inputs": {
    "personas": "...", "virtual_users": "...", "videos": "...", "events": "..."
  }
}
```

`llm_model`을 draft parquet 컬럼이 아니라 사이드카에 두는 이유는 공유 스키마
계약을 건드리지 않기 위해서다. 사이드카는 계보와 **노출 결정 인자**를 함께
담아, 리플레이의 fail-fast 진단 메시지 재료가 된다.

경계 규칙:

- `click_threshold`는 `exposure_args`에 **넣지 않는다.** 리플레이에서 바꾸는
  값이 바로 그것이다.
- `policy_version`도 `exposure_args` 밖에 둔다. 노출에 영향이 없는 라벨이므로
  리플레이에서 자유롭게 바꿀 수 있어야 한다.
- 산출물은 `data/generated/`(gitignore) 아래에 쓰이는 로컬 개발 아티팩트다.

### 2. 리플레이 — 판정 재사용

CLI에 `--replay-drafts <parquet>`을 추가한다. 지정되면:

- `generate_action_log_drafts()`를 **호출하지 않는다**(LLM 0콜).
- draft는 `read_action_log_draft_parquet()`으로, `llm_model`은 같은 디렉터리의
  `action_log_drafts_meta.json`에서 읽는다. 사이드카가 없으면 실패한다
  (계보 없이 event log를 쓰지 않는다).
- 리랭킹·노출 선택은 **재계산한다.** 결정적이므로 같은 인자에서 같은 노출이
  나온다. 리플레이가 건너뛰는 것은 LLM 판정뿐이다.
- quarantine 파일은 리플레이에서 빈 목록으로 기록된다(이번 실행에서 새로
  격리된 것이 없다는 사실을 그대로 표현).

**인자 처리.** `--seed` / `--k` / `--exploration-ratio` / `--as-of` /
`--generator`의 argparse 기본값을 `None` 센티넬로 바꿔 "명시 여부"를 구분한다
(비리플레이 실행에서는 기존 기본값 42 / 10 / 0.1 / 현재 UTC / `openrouter`를
그대로 적용한다 — 기존 동작 무변경).

| 상황 | 동작 |
|---|---|
| 리플레이 + 미명시 | 메타의 `exposure_args`에서 **상속** |
| 리플레이 + 명시했고 메타와 동일 | 진행 |
| 리플레이 + 명시했고 메타와 불일치 | **에러** — 어떤 인자가 어떻게 다른지 출력 |
| 리플레이 + `--generator` 명시 | **에러** — 판정을 재사용하므로 무의미 |

`--as-of` 상속은 선택이 아니라 필수다. 기본값이 "현재 시각"이라 상속이 없으면
리플레이가 **매번** 인자 불일치가 된다.

`--personas` / `--virtual-users` / `--videos` / `--events`는 리플레이에서도
필요하다(노출 재계산에 피처가 쓰인다). 로드된 유저 수가 메타의 `users`와
다르면 에러 — `--max-users`를 빠뜨린 경우를 잡는다.

**커버리지 fail-fast.** 노출된 `(user, video)` 중 draft가 없는 것이 하나라도
있으면 누락 건수와 함께 즉시 실패한다. 조용히 건너뛰면 노출 수가 줄어 CTR
분모가 왜곡되고, "같은 판정 분포에 커트라인을 적용한다"는 캘리브레이션의
전제가 깨진다. 이 fail-fast는 **리플레이 모드에만** 적용하며, 비리플레이
경로의 quarantine 관용 동작(`dropped_exposures_without_judgment`)은 유지한다.

### 3. `main()` 계약

파일 IO를 CLI 어댑터에 두는 기존 패턴을 지켜, `main()`은 로드된 객체를 받는다.

```python
@dataclass(frozen=True)
class DraftReplay:
    """저장된 LLM 판정과 그 계보 — 항상 함께 다뤄야 하므로 한 값으로 묶는다."""

    drafts: list[ImpressionDraft]
    llm_model: str
    exposure_args: Mapping[str, object]
```

```python
def main(
    personas, virtual_users, videos_raw, events,
    generator: ActionLogGenerator | None = None,
    reranker: Reranker | None = None,
    *,
    replay: DraftReplay | None = None,
    ...
) -> dict:
```

`generator`와 `replay`는 **정확히 하나만** 주어져야 한다. 둘 다이거나 둘 다
아니면 `ValueError`.

리포트(JSON·HTML)에는 `replay: true|false`와 `llm_model`을 추가한다. 어떤
라운드가 원본 판정이고 어떤 라운드가 커트라인만 바꾼 재실행인지 산출물만 보고
구분할 수 있어야 한다. `--log-mlflow` 사용 시 `replay`를 파라미터로 남긴다.

## 캘리브레이션 의미론 — 무엇을 목표 CTR로 삼는가

클릭 게이트는 정책별이 아니라 **합집합 슬레이트 단위**다:
`select_clicks_per_slate(draft_result.drafts, click_threshold)`가 유저별
propensity 최고 영상 1개만 커트라인과 비교한다. 그 영상이 baseline에만 있으면
model 정책의 클릭은 0이 된다.

즉 **커트라인이 직접 통제하는 것은 "몇 명의 유저가 아예 클릭하는가"이고,
정책별 CTR은 거기에 노출 겹침이 곱해진 파생값이다.** 따라서:

- 캘리브레이션은 **합집합 draft 전체**에 적용한다(도구의 기본 동작 그대로).
  `achieved_ctr`은 합집합 노출 기준이며, 리포트의 `policies.*.ctr`과 분모가
  다르다.
- 정책별 실측 CTR은 **리플레이 실행 후 리포트에서 확인한다.** 원하는 수준과
  다르면 `--target-ctr`을 조정해 다시 돌린다. 리플레이는 LLM 0콜이므로 이
  반복은 사실상 공짜다.

`select_clicks_per_slate`를 정책별로 분리하면 정책별 CTR을 직접 통제할 수
있지만, "같은 `(user, video)`는 같은 판정을 공유한다"는 라운드 설계(attribution
오염 방지)를 깨는 동작 변경이므로 이 spec의 범위 밖이다.

**규모 하한.** `recommend_click_threshold`는 `n_click = max(1, round(target_ctr
× impressions))`이므로, 목표 CTR이 1클릭 단위보다 작으면 오버슛한 채 성공한다.
`--max-users 2`(스모크 규모)면 합집합 노출이 ~30건이라 목표 0.015에서
`round(0.015 × 30) = 0` → 1로 올림되어 실제 3.3%가 된다. 1.5%를 의미 있게
맞추려면 클릭 1건당 ~67 노출이 필요하므로 **판정 라운드는 `--max-users 8~10`**
을 권장한다(유저당 합집합 ~15건 → 120~150 노출 → 2클릭 ≈ 1.3~1.7%).

## 운영 절차

```bash
# ① LLM 판정 1회 — 유일한 유료 단계
uv run --env-file .env python -m src.pipeline.simulate_policy_round \
  --personas data/generated/demo_subset/personas.csv \
  --virtual-users data/generated/demo_subset/virtual_users.parquet \
  --videos data/generated/demo_subset/videos.csv \
  --events data/generated/demo_subset/events.csv \
  --generator openrouter --click-threshold 0.5 --max-users 8 \
  --output-dir data/generated/round_a

# ② 캘리브레이션 — LLM 0콜
uv run python -m autoresearch.jobs.click_threshold_calibrate \
  --draft-path data/generated/round_a/action_log_drafts.parquet \
  --target-ctr 0.015

# ③ 추천 커트라인으로 리플레이 — LLM 0콜
uv run --env-file .env python -m src.pipeline.simulate_policy_round \
  --personas data/generated/demo_subset/personas.csv \
  --virtual-users data/generated/demo_subset/virtual_users.parquet \
  --videos data/generated/demo_subset/videos.csv \
  --events data/generated/demo_subset/events.csv \
  --replay-drafts data/generated/round_a/action_log_drafts.parquet \
  --click-threshold <추천값> --max-users 8 \
  --output-dir data/generated/round_a_calibrated
```

②③을 반복해 `policy_round_report.json`의 `policies.model.ctr` /
`policies.baseline.ctr`이 원하는 수준이 될 때까지 조정한다.

`OPENROUTER_API_KEY`는 비대화형 셸에서 `~/.bashrc`가 읽히지 않으므로 ①에만
주입한다: `eval "$(grep '^export OPENROUTER_API_KEY=' ~/.bashrc)" && ...`.
②③은 키가 필요 없다.

## 테스트

`tests/test_simulate_policy_round.py`에 추가한다(모두 `RuleBasedActionLogGenerator`
사용, LLM 호출 없음).

1. 덤프 — 실행 후 `action_log_drafts.parquet`과 메타 JSON이 생기고, parquet이
   `ACTION_LOG_DRAFT_PARQUET_SCHEMA`로 다시 읽히며, 메타의 `llm_model`·
   `exposure_args`가 실행 인자와 일치한다.
2. 리플레이 왕복 — 같은 `click_threshold`로 리플레이하면 원본과 **동일한**
   event log가 나온다(이벤트 수·클릭 키셋·정책별 CTR 동등).
3. 커트라인 변경 — 커트라인을 올리면 클릭이 줄어든다(판정 재사용 확인).
4. 커버리지 부족 — draft 1건을 지운 parquet으로 리플레이하면 누락 건수를 담은
   에러로 실패한다.
5. 인자 불일치 — 메타와 다른 `--seed`를 명시하면 차이를 담은 에러로 실패한다.
6. 인자 상속 — `--seed`/`--as-of` 미명시 리플레이가 메타 값을 상속해 성공한다.
7. 계보 — 리플레이 event log의 `llm_model`이 메타 값과 같다.
8. `main()` 계약 — `generator`와 `replay`를 둘 다 주거나 둘 다 안 주면
   `ValueError`.

## 범위 밖

- `autoresearch/action_logs/` 공유 스키마(`ACTION_LOG_DRAFT_PARQUET_SCHEMA`,
  event log 스키마) 변경
- `daily.py`의 shard/merge 경로 변경
- `select_clicks_per_slate`의 정책별 분리
- GCS 업로드 (덤프는 로컬 출력 디렉터리 전용)
