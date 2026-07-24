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
  "exposure_keys": {
    "user_1": ["video_12", "video_3", "video_7"],
    "user_2": ["video_1", "video_9"]
  },
  "policy_version": "local",
  "virtual_users": 100,
  "users": 100,
  "drafts": 1483,
  "inputs": {
    "personas": "...", "virtual_users": "...", "videos": "...", "events": "..."
  }
}
```

`llm_model`을 draft parquet 컬럼이 아니라 사이드카에 두는 이유는 공유 스키마
계약을 건드리지 않기 위해서다. 사이드카는 계보와 **노출 결정 인자**를 함께
담아, 리플레이의 fail-fast 진단 메시지 재료가 된다.

`exposure_keys`(#274 추가)는 판정 라운드의 **유저별 합집합 노출 video_id
목록**(정렬)이다. draft parquet은 격리된 청크의 판정을 담지 않으므로,
"무엇이 노출되었어야 했는가"는 사이드카만이 안다. 리플레이 커버리지 검사가
이 집합과의 정확 비교로 동작한다(아래 절 참조).

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
필요하다(노출 재계산에 피처가 쓰인다). 로드된 유저 수가 메타의 `virtual_users`와
다르면 에러 — `--max-users`를 빠뜨린 경우를 잡는다. (`users`는 persona 누락으로 건너뛴 유저를 제외한 수라
입력 규모 비교에는 `virtual_users`를 쓴다.)

**커버리지 fail-fast는 원본 노출 키 집합과의 정확 비교다(#274).** 사이드카에
`exposure_keys`가 있으면(신규 덤프는 항상 기록) 이번 실행의 유저별 노출
video_id 집합을 원본과 직접 비교한다:

- 유저 집합이 다르면 실패한다(어느 쪽에만 있는 유저인지 출력) — 유저 수는
  같고 id만 다른 파일로 리플레이하는 "전원 zero-coverage 은폐"까지 직접
  검출한다.
- 어느 유저든 노출 video_id 집합이 다르면 실패한다(첫 유저의 차이 예시 출력).
- 두 비교가 통과하면, 판정 없는 노출은 **전부 원본 라운드에서 quarantine된
  것**으로 확정되므로(유저 전체 격리든 `chunk_size > 0`의 청크 부분 격리든)
  비리플레이 경로와 동일하게 관용하고 `dropped_exposures_without_judgment`로만
  계수한다 — 원본 라운드의 quarantine 분모를 그대로 재현하기 위해서다.

**구버전 사이드카 폴백.** `exposure_keys`가 없는 사이드카(이 필드 도입 전
덤프)는 유저(슬레이트) 단위 휴리스틱으로 검사한다: draft가 하나도 없는
유저는 원본 quarantine으로 관용·계수하고, 일부만 있는 유저는 노출 집합
불일치 신호로 실패한다. 이 휴리스틱은 `chunk_size > 0`의 부분 격리 라운드를
리플레이하지 못하는 한계가 있다(#274) — 필요하면 판정 라운드를 다시 덤프한다.

**관용 규칙의 전제는 따로 검사한다.** 위 첫 번째 규칙은 "draft가 없는 유저 =
원본에서 quarantine된 유저"라고 가정하는데, 그 가정이 깨지면 관용이 곧 은폐가
된다. 유저 수는 같고 id만 다른 virtual user 파일로 리플레이하면 **전원이**
zero-coverage가 되어 아무도 부분 커버리지에 걸리지 않고, 전 노출이 `dropped`로
빠져 `impressions = 0` · `CTR = 0` 리포트가 에러 없이 산출된다.

그래서 커버리지 검사에 **앞서**, 판정이 있는 유저가 전부 이번 노출에도
나타나는지 확인한다(`{d.user_id for d in drafts} - set(exposures_by_user)`).
하나라도 빠지면 유저 집합이 판정 라운드와 다른 것이므로 실패한다. quarantine된
유저는 노출에는 있고 draft에만 없으므로 이 검사(반대 방향)에 걸리지 않는다.
이 검사는 사이드카 버전과 무관하게 항상 수행한다 — `exposure_keys`가 있는
신규 경로에서는 정확 비교가 유저 집합 차이를 어차피 잡지만, 구버전 폴백
경로에서는 이 검사가 유일한 유저 집합 방어선이다.

유저 전체가 quarantine된 경우까지 실패로 처리하면, 실 LLM 판정 라운드에서
quarantine이 한 명이라도 나온 순간 그 라운드의 draft parquet은 영원히
리플레이할 수 없게 된다(quarantine은 드물지 않다). 유저 단위로 나눈 이유가
이것이다. 이 fail-fast는 **리플레이 모드에만** 적용하며, 비리플레이 경로의
quarantine 관용 동작은 유지한다.

**노출 인자 검증은 `main()` 자체에도 있다.** `resolve_exposure_args`에 의한
상속·불일치 검사는 `_cli()` 계층에만 있어, `main()`을 직접 호출하는 경로
(테스트·후속 배치·노트북)에서는 불변식이 강제되지 않았다. `main()`은 리플레이
분기에서 `replay.exposure_args`와 이번 실행의 실제 노출 인자
(`seed`/`k`/`exploration_ratio`/`as_of`)를 비교해, 다르면 어떤 인자가 어떻게
다른지 담아 `ValueError`를 던진다. 이 검사가 먼저 걸리므로, 위 유저 단위
커버리지 검사가 실패하는 경우는 사실상 노출 인자가 아니라 유저 집합 자체가
다른 경우로 좁혀진다.

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

**규모 하한.** `recommend_click_threshold`는
`n_click = min(users, max(1, round(target_ctr × impressions)))`이므로, 목표
CTR이 1클릭 단위보다 작으면 오버슛한 채 성공한다. `--max-users 2`(스모크
규모)면 합집합 노출이 ~30건이라 목표 0.015에서 `round(0.015 × 30) = 0` → 1로
올림되어 실제 3.3%가 된다. 1.5%를 의미 있게 맞추려면 클릭 1건당 ~67 노출이
필요하다.

**판정 라운드는 데모 서브셋 전량(`--max-users 100`)으로 돌린다.** 유저당 합집합
~15건 → ~1,500 노출 → `n_click ≈ 22`가 되어 커트라인 해상도가 충분하고,
`sweep` 진단도 의미 있는 분포를 갖는다. 리플레이는 LLM 0콜이므로 이 비용은
라운드당 1회만 든다.

`recommend_click_threshold`는 `target_ctr`이 CTR 상한(`users / impressions`,
100유저·1,500노출 기준 ≈ 0.067)을 넘으면 `ValueError`로 거부한다. 슬레이트당
최대 1클릭 계약에서 나오는 구조적 상한이다.

## 운영 절차

```bash
# ① LLM 판정 1회 — 유일한 유료 단계
uv run --env-file .env python -m src.pipeline.simulate_policy_round \
  --personas data/generated/demo_subset/personas.csv \
  --virtual-users data/generated/demo_subset/virtual_users.parquet \
  --videos data/generated/demo_subset/videos.csv \
  --events data/generated/demo_subset/events.csv \
  --generator openrouter --click-threshold 0.5 --max-users 100 \
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
  --click-threshold <추천값> --max-users 100 \
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
4. 유저 슬레이트 부분 커버리지 — 한 유저의 draft 중 일부만 지운 parquet으로
   리플레이하면(그 유저는 draft가 남아 있으되 전부는 아님) 실패한다.
5. 유저 전체 quarantine 관용 — 한 유저의 draft를 전부 지운 parquet으로
   리플레이하면 성공하고, 그 유저의 노출은 `dropped_exposures_without_judgment`로
   계수된다.
6. 인자 불일치 — 메타와 다른 `--seed`를 명시하면 차이를 담은 에러로 실패한다.
7. `main()` 노출 인자 검증 — `main()`을 직접 호출해 메타와 다른 `k`로
   리플레이하면(예: 판정 라운드 `k=6` → 리플레이 `k=3`) `ValueError`로
   실패한다(`_cli()`를 거치지 않는 경로에서도 불변식이 강제됨을 확인).
8. 인자 상속 — `--seed`/`--as-of` 미명시 리플레이가 메타 값을 상속해 성공한다.
9. 계보 — 리플레이 event log의 `llm_model`이 메타 값과 같다.
10. `main()` 계약 — `generator`와 `replay`를 둘 다 주거나 둘 다 안 주면
    `ValueError`.
11. (#274) 사이드카 기록 — 덤프 메타의 `exposure_keys`가 노출 유저 전원을
    담고 유저별 목록이 정렬되어 있다.
12. (#274) 부분 draft 관용 — `exposure_keys`가 있으면 유저의 draft 일부
    누락(청크 부분 격리 재현)을 관용하고 dropped로 계수한다.
13. (#274) 노출 집합 불일치 — 원본 노출 키 집합과 다른 리플레이는 유저·차이
    예시를 담은 에러로 실패한다.
14. (#274) 유저 집합 불일치 — 노출 유저 집합이 다른 리플레이는 실패한다.
15. (#274) 부분 격리 왕복 — `chunk_size=1`에서 특정 청크만 실패하는 판정기로
    부분 quarantine 라운드를 만들고, 같은 노출 인자의 리플레이가 성공하며
    격리분이 dropped로 계수됨을 확인한다. 4·5번 기존 테스트는 구버전 사이드카
    폴백 경로의 회귀 테스트로 유지한다.

## 범위 밖

- `autoresearch/action_logs/` 공유 스키마(`ACTION_LOG_DRAFT_PARQUET_SCHEMA`,
  event log 스키마) 변경
- `daily.py`의 shard/merge 경로 변경
- `select_clicks_per_slate`의 정책별 분리
- GCS 업로드 (덤프는 로컬 출력 디렉터리 전용)

## 실행 결과 (2026-07-23)

폐루프를 실제 OpenRouter LLM으로 완주해 검증했다. **단, 계획한 100유저가 아니라
8유저로 축소해 실행했다** — 사유는 아래 "규모 제약"에 적는다.

### 라운드 요약

| 항목 | 값 |
|---|---|
| 판정 라운드 유저 | 8 (`--max-users 8`) |
| LLM 모델 | `mistralai/mistral-nemo` |
| 합집합 draft(=캘리브레이션 분모) | 152 |
| 정책별 노출 | baseline 80 / model 80 |
| 노출 겹침 (Jaccard 평균) | 0.0548 |
| quarantine | 0 |

### ① 판정 라운드 (`--click-threshold 0.5`, 유일한 유료 단계)

| 정책 | impressions | clicks | CTR | mean propensity |
|---|---|---|---|---|
| baseline | 80 | 0 | 0.0% | 0.2250 |
| model | 80 | 6 | 7.5% | 0.2394 |

산출물: `action_log_drafts.parquet`(152행), `action_log_drafts_meta.json`,
`event_log.parquet`, 리포트 JSON·HTML.

### ② 캘리브레이션 (`--target-ctr 0.015`, LLM 0콜)

```
recommended_threshold = 0.7
achieved_ctr          = 0.0132   (합집합 152 노출 기준)
users = 8, impressions = 152
per_user_max_quantiles = {p50: 0.5, p75: 0.6, p90: 0.7, p95: 0.8, p99: 0.8}
sweep = [(0.4, 0.0526), (0.5, 0.0395), (0.6, 0.0197), (0.7, 0.0132), (0.8, 0.0066)]
```

### ③ 리플레이 (`--click-threshold 0.7`, LLM 0콜)

| 정책 | impressions | clicks | CTR |
|---|---|---|---|
| baseline | 80 | 0 | 0.0% |
| model | 80 | 2 | 2.5% |

리포트에 `replay: true`, `llm_model: mistralai/mistral-nemo`가 기록되었고,
`dropped_exposures_without_judgment = 0`, 리플레이 event log의 `llm_model`도
원본 판정 모델과 일치했다. 노출 인자(`seed`/`k`/`exploration_ratio`/`as_of`)는
CLI에 다시 주지 않고 사이드카 메타에서 상속되었다.

### 관측 — 합집합 기준과 정책별 기준의 분모 차이

캘리브레이션의 `achieved_ctr`(1.32%)과 리플레이의 `policies.model.ctr`(2.5%)이
다른 것은 정상이며, 이 spec의 "캘리브레이션 의미론" 절이 예측한 그대로다.
클릭 2건은 동일하고 분모만 다르다 — 합집합 152 노출 대 정책별 80 노출.
목표 CTR을 정책별 기준으로 맞추려면 리플레이 실측을 보고 `--target-ctr`을
조정하는 반복이 필요하며, 리플레이가 LLM 0콜이므로 그 반복 비용은 없다.

또한 커트라인 0.5 → 0.7에서 model 클릭이 6 → 2로 줄고 baseline은 0으로 유지된
것은, 커트라인이 "몇 명이 아예 클릭하는가"를 통제하고 정책별 CTR은 그 결과에
노출 겹침이 곱해진 파생값이라는 설계와 일치한다.

### 규모 제약 — 100유저를 돌리지 못한 이유

`--max-users 100`과 `--max-users 25` 모두 **Vertex AI 임베딩 할당량**에 막혀
실패했다(429 `Quota exceeded for aiplatform.googleapis.com/online_prediction_requests_per_base_model`,
base model `textembedding-gecko`). 실패 지점은 `build_pool_feature_frame` →
`compute_interaction_columns` → `embed_texts`(`src/features/embeddings.py`)로,
**노출 피처 계산 단계이며 OpenRouter 호출 이전**이다. 따라서 실패한 두 시도에서
LLM 비용은 발생하지 않았다.

원인은 이 경로가 **유저 1명당 임베딩 요청 1건**을 내는 데 있다. 단일
`embed_texts` 호출은 정상 동작하므로 할당량은 소진이 아니라 분당 요율 제한이며,
`_get_embeddings_chunk`의 재시도는 3회·최대 20초 백오프라 분당 창이 회복되기
전에 소진된다.

이 제약은 이 spec의 범위 밖이다. 100유저 규모의 캘리브레이션이 필요하면 다음
중 하나를 별도 이슈로 다뤄야 한다:

- 프로세스 수준 임베딩 캐시 — 페르소나 간 중복 키워드가 많아 요청 수를 크게
  줄일 수 있다 (`src/features/embeddings.py`는 다른 파이프라인도 공유하므로
  영향 범위 검토 필요)
- 유저 루프의 요율 제한 또는 재시도 백오프 상향
- Vertex AI 할당량 증설 요청

8유저(합집합 152 노출)에서도 `n_click = round(0.015 × 152) = 2`로 캘리브레이션
자체는 성립했으나, `sweep`의 커트라인 후보가 0.4~0.8의 5개뿐이라 해상도는
100유저 대비 낮다.

## 해소된 한계 — `chunk_size > 0`의 부분 quarantine (#274, 2026-07-24)

이 절의 한계는 #274에서 해소되었다. 원래 리플레이의 커버리지 규칙은 "draft가
하나도 없는 유저 = 원본 라운드의 quarantine(관용), 일부만 있는 유저 = 노출
집합 불일치(실패)"라는 이분법에 기댔는데, `_generate_drafts_isolated`의 격리
단위는 **(유저 × 후보 청크)**이므로 `chunk_size > 0`에서는 같은 유저의 청크
일부만 격리될 수 있고, 그 라운드는 같은 노출 인자로 리플레이해도 "부분
커버리지"로 오판되어 항상 실패했다.

해소 방식: 덤프가 사이드카에 `exposure_keys`(유저별 합집합 노출 video_id
집합)를 기록하고, 리플레이 커버리지를 그 집합과의 **정확 비교**로 바꿨다
("리플레이 — 판정 재사용" 절 참조). 노출 집합이 원본과 같으면 미판정 노출은
전부 원본 격리로 확정되므로 관용해도 은폐가 아니고, 다르면 격리 구간에 국한된
차이까지 검출된다. `exposure_keys`가 없는 구버전 사이드카는 기존 휴리스틱으로
폴백하므로, 그 덤프의 `chunk_size > 0` 리플레이가 필요하면 판정 라운드를 다시
덤프해야 한다.

`chunk_size`는 노출을 바꾸지 않으므로 여전히 `exposure_args`에 넣지 않는다.
테스트 11~15가 이 경로를 덮는다(부분 격리 재현 라운드의 리플레이 성공 포함).
