# 슬레이트당 최대 1클릭 · 관련성 커트라인 클릭 계약 (SP-1)

> 작성: 2026-07-22 | 상태: 설계(리뷰 대기) | 선행: #216·#219·#221·#222(머지 완료)

## 목표

오프라인 시뮬레이션 폐루프에서 **클릭을 정하는 방식**을 바꾼다.

- **AS-IS:** 전역 CTR 할당량(`target_ctr`, 기본 2%). 모든 유저·모든 노출을
  한 통에 모아 `click_propensity` 상위 `round(target_ctr × N)`개를 클릭으로
  도장 찍는다. → 모델이 아무리 좋아져도 CTR이 **2%에 고정**된다.
- **TO-BE:** **사람(슬레이트)당 최대 1클릭 + 관련성 커트라인**. 유저가 본
  목록에서 `click_propensity`가 가장 높은 영상 1개가 고정 커트라인을 넘으면
  클릭, 못 넘으면 클릭 0개. → CTR이 **모델 실력에 따라 움직인다.**

이 변경의 핵심 의도는 "사람당 1개"가 아니라, **정해진 할당량(2%)을 없애고
관련성 기준으로 바꿔 CTR이 모델 개선을 반영하도록 만드는 것**이다.

## 배경 — 왜 바꾸나

폐루프의 목적은 "모델이 좋아지면 CTR이 오른다"를 관측하는 것이다. 그러나
현재 전역 2% 정규화는 **클릭 수를 항상 전체의 2%로 강제**하므로, 모델이
개선돼도 CTR 숫자는 2%에 얼어붙어 개선이 지표에 보이지 않는다. 전역 할당량은
초기 부트스트랩용 임시 장치였으며, 이 SP-1에서 은퇴시킨다.

관련성 커트라인 방식에서는:

- **AI(LLM)는 고정된 관련성 심판**으로 남는다(영상별 `click_propensity`만
  판단, 이 SP-1에서 프롬프트·응답 포맷 변경 없음).
- **모델의 임무는 AI 심판 앞에 더 잘 맞는 영상을 놓는 것**이다.
- 모델이 좋아짐 → 최고 점수가 커트라인을 넘는 유저가 늘어남 → 클릭 증가 →
  **CTR 상승.** 커트라인과 심판이 고정이므로 CTR 변화는 모델 실력을 공정하게
  반영한다.

## 현재 구조 (AS-IS)

- LLM은 후보별 `[index, click_propensity, watch_fraction]`을 반환한다
  (`action_logs/llm_generator.py`). 이 SP-1에서 **변경하지 않는다.**
- `_build_user_drafts()`가 후보별 `ImpressionDraft`를 만든다
  (`click_propensity`, `watch_fraction`, `would_like`, `duration_sec`).
- 클릭 선정은 전역 정규화:
  - `_clicked_indices(drafts, target_ctr)` /
    `normalize_clicks(drafts, target_ctr)`
    (`action_logs/pipeline.py:677`)이 전체 draft를
    `(-click_propensity, user_id, video_id)`로 정렬해 상위
    `round(target_ctr × N)`개를 클릭으로 선정.
  - 호출처: `action_logs/pipeline.py`(런당 1회), `simulate_policy_round.py`
    (baseline·model 합동 pool에 1회).
- 클릭된 draft에만 `click`/`view`(watch_time)/`like` 이벤트가 추가된다.
  `watch_time`은 `watch_fraction × duration`, `like`는
  `derive_would_like(click_propensity, watch_fraction)`
  (임계값 0.7/0.6)로 파생된다. **이 파생 로직은 변경하지 않는다.**
- `target_ctr`(기본 0.02)는 다음에 실처럼 꿰여 있다:
  `action_logs/schema.py`(요청/manifest 필드), `action_logs/daily.py`,
  `action_logs/pipeline.py`, `jobs/action_log.py`(CLI `--target-ctr`),
  `src/pipeline/report_html.py`, `src/pipeline/simulate_policy_round.py`.

## 변경 계약 (TO-BE)

### 클릭 결정 규칙

슬레이트 = **한 유저의 노출 draft 묶음(한 런 안에서)**. 유저 단위로:

1. 그 유저의 draft 중 `click_propensity`가 최대인 draft 1개를 고른다.
2. 그 최대값이 `CLICK_THRESHOLD` **이상(≥)**이면 → 그 draft를 **클릭**으로 선정.
3. 못 넘으면 → 그 유저는 **클릭 0개**.
4. 최대값 동점은 결정적 tiebreak로 깨뜨린다: 기존과 동일하게
   `(-click_propensity, user_id, video_id)` 순서의 첫 draft.

방식은 **결정적 하드 컷오프**(확률적 동전 던지기 아님). 같은 입력 → 같은 클릭.

### 커트라인 (`CLICK_THRESHOLD`)

- **관련성 점수(0~1)에 거는 고정 커트라인.** 할당량이 아니다.
- **한 번 정하면 모델 이터레이션 전체에서 고정**한다. 그래야 CTR 변화가
  "모델 실력"만을 반영한다.
- 단일 출처: 기존 `target_ctr`가 쓰던 슬롯을 그대로 승계한다 — 즉
  `autoresearch/action_logs`가 클릭 계약의 소유자이므로 기본 상수와 스키마
  기본값을 여기에 두고, `simulate_policy_round`는 (이미 `normalize_clicks`를
  이 패키지에서 import하듯) 같은 출처를 가져와 쓴다. 두 곳에 숫자를 분산하지
  않는다.
- 출발점 권장: `0.5~0.6`. 기존 `like` 커트라인(0.7)보다 낮게(클릭이 좋아요보다
  헐거우므로).

### 캘리브레이션 절차 (커트라인 값 확정)

1. 현재 **기본(champion) 모델로 폐루프를 1회** 돌린다.
2. 유저별 "최고 `click_propensity`" 분포를 집계한다.
3. **기본 모델 CTR이 1~2%**가 되는 커트라인을 고른다(자랄 여유 확보).
4. 그 값을 상수에 고정한다. 이후 재캘리브레이션은 명시적 결정으로만 한다.

근거: 슬레이트 크기 `S`일 때 슬레이트당 최대 1클릭이므로 **CTR 천장 = 1/S**
(24개면 약 4.2%). 기본을 1~2%로 시작하면 천장까지 상승 여유가 남는다.

### 제거·대체

- `_clicked_indices`/`normalize_clicks`의 전역 상위 `round(ctr×N)` 로직을
  **유저별 최고 1개 + 커트라인** 선정으로 교체한다.
- `target_ctr`(스키마 필드·CLI·리포트·시뮬 인자)를 **`click_threshold`로
  교체**한다. 정책 시뮬(`simulate_policy_round`)도 동일 방식으로 전환한다
  (두 방식 공존시키지 않음 — 완전 교체).

## 영향 범위 (코드)

| 파일 | 변경 |
| --- | --- |
| `autoresearch/action_logs/pipeline.py` | 전역 선정 → 유저별 최고+커트라인 선정. `normalize_clicks`/`_clicked_indices` 계약 교체 |
| `autoresearch/action_logs/schema.py` | 요청/manifest의 `target_ctr` → `click_threshold` |
| `autoresearch/action_logs/daily.py` | `target_ctr` 스레딩 → `click_threshold` |
| `autoresearch/jobs/action_log.py` | CLI `--target-ctr` → `--click-threshold` |
| `src/pipeline/simulate_policy_round.py` | `normalize_clicks(target_ctr)` → 유저별 커트라인 선정 |
| `src/pipeline/report_html.py` | 리포트의 `target_ctr` 표기 → `click_threshold` |

`llm_generator.py`, `derive_would_like`, watch_time/like 파생, 노출 조립
(`model_exposure_provider`)은 **변경하지 않는다.**

## 문서 갱신 원칙 (사용자 우려 반영)

문서 스캔 결과 `target_ctr`/전역 2%를 언급하는 문서가 다수지만, **동결 문서와
살아있는 문서를 구분**해 살아있는 것만 갱신한다.

- **갱신 대상(살아있는 권위 문서):**
  - `docs/guides/action-log.md`
  - `docs/guides/agent-simulator-spec.md`
  - `docs/specs/2026-07-20-policy-simulation-round.md`(클릭 계약 부분)
  - `docs/specs/2026-07-22-daily-closed-loop.md`(참조·정합)
  - `docs/README.md`(색인에 클릭 계약 언급 시)
- **동결(수정 금지):**
  - `docs/archive/**`(과거 계획·스펙·리포트 — 시점 기록)
  - `docs/**/pr*_report.html`, 기타 자동 생성 리포트 HTML(스냅샷)
  - `docs/*.html` 시각화(별도 재생성 대상이면 재생성으로만 갱신)

즉 **~40개 전부가 아니라 살아있는 문서 5개 안팎만** 손본다.

## 테스트 계획

- **교체할 테스트:** `tests/test_action_logs_pipeline.py`,
  `tests/test_action_logs_daily.py`, `tests/test_simulate_policy_round.py`의
  `target_ctr`/전역 2% 전제 단정.
- **추가할 테스트:**
  - 유저별 최고 1개만 클릭됨(다른 draft는 impression만).
  - 최고 점수가 커트라인 미만이면 그 유저 클릭 0개.
  - 커트라인 경계값(정확히 커트라인이면 클릭 — `≥`).
  - 동점 tiebreak 결정성.
  - CTR이 고정이 아니라 입력(점수 분포)에 따라 달라짐(할당량 부재 검증).
  - 클릭된 영상의 watch_time/like 파생이 기존과 동일.

## 범위 밖 (다른 하위 프로젝트)

- **SP-2 노출 경로:** 온라인 top-K 서버(PostProcess)·csv/parquet 파일 출력.
  이 폐루프는 오프라인 배치/BQ 경로를 유지하므로 여기서 다루지 않는다.
- **SP-3 승격 완결:** end-to-end 검증 및 backfill/휴리스틱 노출 소스 은퇴.
- 멀티태스크 모델(watch_time/like 예측)·`/rerank` 응답 확장.
- `derive_would_like` 임계값(0.7/0.6) 재조정.

## 리스크 · 롤백

- **CTR이 창발값이 된다.** 2% 고정을 전제하던 리포트·지표·테스트를 함께
  갱신해야 한다(위 테스트 계획 참조).
- **캘리브레이션 민감도:** 커트라인이 어긋나면 기본 CTR이 0%나 천장에 붙을
  수 있다. 커트라인은 설정 상수이므로 재캘리브레이션으로 조정한다.
- **manifest 스키마 변경:** `target_ctr` → `click_threshold`는 직렬화 계약
  변경이다. 기존 manifest 소비 지점을 함께 확인한다.
- **롤백:** 클릭 선정 함수와 `click_threshold` 스레딩을 되돌리고 `target_ctr`
  전역 정규화를 복원하면 된다(단일 개념의 역변경).

## 완료 기준

- 클릭은 유저별 최대 1개이며, 최고 점수가 커트라인 미만인 유저는 클릭 0개다.
- 전역 `target_ctr` 정규화가 코드·CLI·스키마·리포트·정책 시뮬에서 제거된다.
- CTR은 고정이 아니라 점수 분포(모델 실력)에 따라 변한다(테스트로 고정).
- 클릭된 영상의 watch_time/like 파생 동작은 불변이다.
- 살아있는 문서 5개 안팎이 새 계약으로 갱신되고, 동결 문서는 손대지 않는다.
