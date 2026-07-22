# click_threshold fail-closed 전환 + 캘리브레이션 분석 헬퍼 (#260)

> 작성: 2026-07-22 | 상태: 설계(리뷰 대기) | 선행: #255(PR #259, per-slate 클릭 커트라인)

## 목표

#255가 도입한 `click_threshold`의 **fail-open(기본값 0.55로 조용히 실행)**을
**fail-closed(명시 안 하면 실패)**로 바꾸고, 커트라인 값을 정하는 **분석
헬퍼(A)**를 제공한다. 근거는 PR #259의 claude-review 봇 지적 3건이다.

## 배경 — 봇 리뷰 지적

- **미캘리브레이션 blind 실행 (daily.py):** 커트라인은 "유저가 본 것 중 최고
  `click_propensity` 1개"에 걸린다. AI가 대부분 영상을 낮게 채점하므로, 기본
  0.55로 운영 daily를 돌리면 대부분 유저의 최고값이 0.55 미만 → **클릭
  0건(CTR≈0%)** → 전부 0클릭의 무의미한 action log 위험.
- **구버전 manifest 조용한 채움 (schema.py):** 구버전 shard(`target_ctr`만
  기록)를 신버전 merge가 역직렬화하면(`extra="ignore"`) `click_threshold`가
  조용히 0.55로 채워진다.
- **(부수) 합동 pool 클릭 의미론 (simulate_policy_round.py):** 정책 시뮬은
  두 정책 노출의 합집합에 per-slate 선정 1회 → 유저별 최고 1건이 승자.

## Part 1 — fail-closed (기본값 제거)

`click_threshold` 기본값 `0.55`를 제거해 **명시 필수**로 만든다. 안 주면
조용히 돌지 않고 **명확히 실패**한다.

### 제거 대상 (8곳)

| 위치 | 현재 | 변경 |
| --- | --- | --- |
| `autoresearch/action_logs/schema.py:171` | `EventGenerationRequest.click_threshold: float = 0.55` | 기본값 제거(required) |
| `autoresearch/action_logs/schema.py:126` | `ActionLogShardManifest.click_threshold: float = Field(default=0.55, ...)` | `Field(ge=0.0, le=1.0)` (default 제거, required) |
| `autoresearch/jobs/action_log.py:133` | CLI `--click-threshold ... default=0.55` | `required=True`, default 제거 |
| `autoresearch/action_logs/daily.py:824,976` | `click_threshold: float = 0.55` | 기본값 제거(required kwarg) |
| `src/pipeline/simulate_policy_round.py:138` | `click_threshold: float = 0.55` | 기본값 제거(required kwarg) |
| `src/pipeline/simulate_policy_round.py:326` | CLI `--click-threshold ... default=0.55` | `required=True` |
| `scripts/generate_action_logs_scale.py:49` | `--click-threshold ... default=0.55` | `required=True` |

### 계약

- **request/manifest 필드는 Pydantic required.** `EventGenerationRequest(...)`
  를 `click_threshold` 없이 만들면 `ValidationError`. 정확히는, `click_threshold`
  필드 자체가 없는 **#255 이전 manifest**(구버전 shard가 남긴, `target_ctr`만
  기록된 형태)를 역직렬화하면 `ValidationError`가 난다(cross-version
  fail-closed) — 이 문서 상단 "조용히 0.55로 채워지던" 지적이 가리키는 대상이다.
  반대로 **#255 시점 이후 manifest**는 Pydantic이 `click_threshold: 0.55`를
  이미 명시적으로 직렬화해 두었으므로 필드가 존재하고, 역직렬화도 그대로
  성공한다 — fail-closed는 필드가 아예 없는 구버전만 걸러내며, #255-era
  manifest를 재해석·재검증하지는 않는다.
- **CLI는 `required=True`.** `--click-threshold` 없이 실행하면 argparse가
  즉시 실패(비영 종료).
- 값의 흐름은 불변(CLI → request → manifest). 클릭 선정 로직·per-slate 계약은
  건드리지 않는다.

### 블라스트 반경

기존 테스트·호출부가 `click_threshold` 없이 `EventGenerationRequest(...)`·
`run_daily_*`·`main(...)`를 만들던 곳은 **모두 명시값을 넘기도록** 갱신한다
(기계적). fail-closed가 실제로 걸리는지 확인하는 negative 테스트를 추가한다.

## Part 2 — 캘리브레이션 분석 헬퍼 (A)

이미 생성된 propensity 데이터에서 목표 CTR에 맞는 커트라인을 산출한다. LLM·
모델 없이 순수 계산 → 테스트 가능. **(C) 전체 실행 헬퍼가 나중에 이 순수
함수를 그대로 감싸도록** 입력 계약을 데이터-우선으로 좁힌다.

### 순수 함수

```python
@dataclass(frozen=True, slots=True)
class ThresholdRecommendation:
    recommended_threshold: float   # 목표 CTR을 달성하는 커트라인
    achieved_ctr: float            # 그 커트라인에서의 실제 CTR
    target_ctr: float
    users: int
    impressions: int
    sweep: tuple[tuple[float, float], ...]      # (threshold, ctr) 표
    per_user_max_quantiles: Mapping[str, float] # 예: {"p50":.., "p90":..}

def recommend_click_threshold(
    per_user_max_propensity: Sequence[float],   # 유저별 최고 click_propensity
    impressions: int,                            # 총 노출 수(=CTR 분모)
    target_ctr: float,
) -> ThresholdRecommendation:
    ...
```

- 계약: per-slate 최대 1클릭이므로 `CTR(t) = (max ≥ t 인 유저 수) / impressions`.
  목표 CTR을 위해 `n_click = round(target_ctr × impressions)`명이 클릭해야
  하므로, 유저별 최고값을 내림차순 정렬해 **n_click번째로 큰 값**을 추천
  커트라인으로 한다(그 값 이상인 유저가 대략 n_click명 → CTR≈target).
- 경계: `target_ctr`가 0이면 추천 불가(에러), CTR 천장 `users/impressions`를
  넘는 target도 에러. 빈 입력은 에러.
- 순수·결정적(정렬 tie는 값으로 안정). LLM·파일·IO 없음.

### CLI 어댑터 (public batch job)

`autoresearch/jobs/click_threshold_calibrate.py` (기존
`jobs/action_log_quality.py` 패턴):

- 입력: 생성된 **draft parquet**(`ImpressionDraft`에 `user_id`, `video_id`,
  `click_propensity` 보유; `read_action_log_draft_parquet`로 로드) + `--target-ctr`.
- 처리: draft에서 유저별 최고 `click_propensity`와 총 노출 수를 추출 → 순수
  함수 호출.
- 출력: 추천 커트라인·달성 CTR·sweep·분위수를 **JSON 1줄**로 emit(기존 batch
  job 출력 계약과 동일). 사람이 그 숫자를 보고 확정한다.

### (A) → (C) 이음매

순수 함수는 "유저별 최고값 + 노출 수"만 받으므로 format-agnostic이다. (C)는
나중에 "기본 모델 + LLM 실행 → draft 생성" 앞단만 얹고 같은 순수 함수를
재사용한다. 이번 이슈에서 (C)는 만들지 않는다.

## Part 3 — 문서

- `docs/guides/action-log.md` 또는 신규 운영 노트: **캘리브레이션 절차**
  (기본 모델 1회 실행 → draft 확보 → `click_threshold_calibrate`로 분포·추천
  산출 → 값 확정 → 그 값을 `--click-threshold`로 운영 실행) + **캘리브레이션
  전 운영 실행 금지** 명시.
- fail-closed 계약(기본값 없음, 미지정 시 실패)을 문서화.
- (부수) 합동 pool 클릭 의미론: 유저별 최고 1건 = 합집합 승자; 승자 영상이
  두 정책에 겹치면 공유 판정되어 유저합 최대 2, per-(policy,user)는 1 보장.

## 테스트 계획

- **순수 함수:** 목표 CTR 대응 커트라인 산출(알려진 분포로 정확값), 천장 초과
  target 에러, target=0 에러, 빈 입력 에러, CTR 단조 감소(sweep) 확인.
- **CLI:** draft parquet fixture → 추천 JSON emit(구조·값), 인자 누락 실패.
- **fail-closed negative:** `EventGenerationRequest()` (click_threshold 없음)
  `ValidationError`; CLI `--click-threshold` 누락 시 비영 종료; 구버전 형태
  manifest(dict without click_threshold) 역직렬화 `ValidationError`.
- 기존 테스트: 모든 request/daily/simulate 생성부에 명시 `click_threshold` 추가.

## 범위 밖

- (C) 전체 실행 헬퍼(기본 모델+LLM 오케스트레이션).
- 실제 캘리브레이션 값 확정(운영 실행 결과) — 문서 절차만 제공.
- per-slate 클릭 선정 로직·노출 조립·LLM 프롬프트 변경.

## 롤백

기본값 재부여(`= 0.55`)와 헬퍼 삭제로 되돌린다. per-slate 클릭 계약(#255)은
불변이므로 데이터 롤백 없음.

## 완료 기준

- `click_threshold` 미지정 시 조용히 0.55로 돌지 않고 **명확히 실패**한다
  (request/manifest/CLI 모두).
- 구버전 형태 manifest가 신버전 역직렬화에서 조용히 0.55로 채워지지 않는다.
- `click_threshold_calibrate`가 draft parquet에서 목표 CTR 대응 커트라인을
  산출하고, 순수 함수는 LLM·IO 없이 테스트로 고정된다.
- 캘리브레이션 절차와 "실행 전 필수" 전제가 문서에 명문화된다.
- (C) 마이그레이션을 위한 순수-함수 이음매가 유지된다.
