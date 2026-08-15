# 로컬 Research Harness MVP — verifier 대체 Implementation Plan

> **상태: #769 이슈·브랜치 생성, 확정 결정 승인 완료.** 구현은 아래 Task 순서와
> 검증·동료 리뷰 규칙을 따른다.

**Goal:** 현행 executor의 `verifier`(정적 allowlist 사전 검문)를 로컬 Research
Harness(봉인된 사후 판정 + 자가 피드백)로 대체한다. 에이전트가 저장소를 자유롭게
바꾸며 실험하고, 봉인된 Judge가 CTR 예측 품질을 판정해 그 결과를 다시 에이전트에게
돌려주는 반복 환경을 만든다.

**Architecture:** 에이전트가 **무엇을 고쳤는지 검사하지 않고, 무엇을 냈는지만
계약한다.** candidate는 disposable worktree에서 저장소 전체를 자유롭게 수정한 뒤
고정 진입점 하나로 예측 점수 파일을 산출한다. 봉인된 slate·정답 라벨·지표 구현·ledger는
worktree 바깥의 별도 디렉터리와 별도 프로세스에 있어, 접근 자체가 불가능하다.
따라서 금지 목록이 필요 없어진다.

**Tech Stack:** Python 3.11/3.12, uv, pytest, ruff, pandas/pyarrow, typer

**Spec:** [`docs/specs/2026-08-14-paper-grounded-autonomous-ml-research-harness.md`](../specs/2026-08-14-paper-grounded-autonomous-ml-research-harness.md)
(현재 미커밋 — Task 0에서 결정 반영 후 커밋)

**Issue:** #769

---

## 확정 결정

이 계획의 전제다. spec에 아직 반영되지 않았으므로 Task 0에서 spec을 갱신한다.

| # | 결정 | 근거 |
| --- | --- | --- |
| D1 | **봉인 경계는 예측 점수 파일**이다. candidate가 `predictions.csv`를 산출하고, Judge는 숨긴 정답으로 채점만 한다. | Judge가 candidate 코드를 일절 실행하지 않으므로 봉인이 가장 깨끗하다. 모델 파일 역직렬화 위험도 없다. |
| D2 | **로컬 harness를 신규로 만든다.** 현행 K8s executor는 손대지 않고, 이후 `ExperimentRunner` 구현체로 흡수한다. | spec 4.4의 local-first. 지금 도는 폐루프를 깨지 않는다. |
| D3 | **verifier의 연구 공간 제한(path allowlist, dependency 금지, 변경량 상한)을 폐기한다.** 봉인 자산의 물리적 분리로 대체한다. | spec 4.2. 금지 목록은 봉인이 아니라 "약속"이며, 자율 시스템에서 약속은 보증이 아니다. |
| D4 | **시크릿·자격증명 커밋 차단만 남긴다.** symlink·submodule·파일 크기·생성 데이터(`.csv/.pkl/.parquet`) 제한은 폐기한다. | 현행 `.csv/.parquet` 거부는 spec 4.2가 명시 허용한 "raw 데이터 재조립과 파생 데이터셋"을 그대로 막는다. 파생 데이터는 커밋이 아니라 workspace 산출물로 다루면 위생과 자유가 동시에 성립한다. |
| D5 | **판정 임계값은 고정 %가 아니라 baseline seed 노이즈 σ의 배수로 정의한다.** (구 M1) | 아래 "판정 규칙" 참조. |
| D6 | **`slate_id`를 action log 생성 단계에서 부여한다.** 사후 추론하지 않는다. 과거 파티션은 평가 대상에서 제외한다. (구 M2) | slate 경계는 노출 시점에만 존재하는 사실이고, 사후 추론은 언제나 근사다. NDCG는 slate 단위로 계산되므로 경계가 틀리면 지표가 **조용히** 틀리고 그 위의 모든 자율 판정이 함께 틀린다. 두 정의를 섞으면 D5의 σ 측정이 오염된다. |
| D7 | **평가 데이터는 `slate_id` 도입 이후 생성분부터 사용한다.** 개발·검증용으로는 `RuleBasedActionLogGenerator`로 로컬 생성한다. (구 M3) | 로컬에 action log parquet 스냅샷이 없음을 확인했다. rule-based 생성기는 LLM·API 키 없이 동작하므로 로컬 완주 원칙(D2)과 맞는다. |

### D1이 만드는 인터페이스

candidate가 지켜야 하는 계약은 **단 하나**다.

```text
입력  <workspace>/harness_in/slate.parquet     라벨 없는 평가 slate
출력  <workspace>/harness_out/predictions.csv  slate_id, video_id, score
진입점 python -m autoresearch.cli harness-predict --slate <in> --out <out>
```

이 명령이 동작하는 한 나머지는 전부 자유다 — 피처 재조립, 모델 교체, 의존성 추가,
디렉터리 구조 변경, 학습 코드 재작성 모두 허용된다. 진입점 계약이 곧 allowlist의
대체물이다.

---

## Global Constraints

- **봉인 규칙(가장 중요).** Judge·slate·라벨·ledger는 candidate worktree **바깥**에
  둔다. Judge 프로세스는 **고정 SHA의 harness 체크아웃**에서 실행하며, `cwd`나
  `sys.path`가 candidate worktree를 가리켜서는 안 된다.

  현행 코드가 정확히 이 지점에서 실패한다 —
  `applications/experiment_platform/executor/measurement.py:205`가
  `cwd=config.workspace`로 평가를 돌리고, 주석이 "executor 이미지가 아니라
  workspace에서 돈다(#754)"고 명시한다. 이 계획의 모든 코드는 그 반대여야 한다.
  Task마다 검증 항목에 "candidate 코드가 Judge 경로에서 import되지 않음"을 넣는다.
- **라벨은 candidate에게 어떤 형태로도 노출하지 않는다.** slate 주입 파일에 `clicked`
  컬럼이 없어야 하고, 피드백 payload에도 행 단위 정답이 들어가지 않는다. 집계 지표만
  돌려준다.
- **기존 실행 경로를 건드리지 않는다.** `applications/experiment_platform/**`는 이 계획의
  변경 대상이 아니다. verifier 삭제는 로컬 harness가 검증된 뒤 별도 이슈로 한다.
- **지표 정의를 새로 만들지 않는다.** LogLoss·Brier·PR-AUC·grouped ROC-AUC는
  `autoresearch/model_evaluation/evaluate.py`의 기존 구현을 재사용한다. NDCG·Recall만
  신규다.
- **새 최상위 패키지를 만든다.** `autoresearch/research_harness/` 도입은 CLAUDE.md에
  따라 **같은 PR에서** `README.md`와 `.claude/docs/agent-project-reference.md`를
  갱신해야 한다(Task 6).
- **모듈 docstring 규칙.** 새 모듈마다 전체 파이프라인 기준 담당 구간(및 담당하지 않는
  인접 책임)과 제공 기능을 최상단 docstring에 적는다.
- 커밋 메시지는 `<type>: 한국어 설명` + 본문 + `Refs #<issue>` +
  `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`.

---

## 작업 진행 방식

**이슈는 Task 1개당 1개씩 발행한다.** 여러 Task를 한 이슈에 묶지 않는다. 이슈는 착수
직전에 만들며, 전체 backlog를 미리 발행하지 않는다 — 앞선 Task의 결과가 뒤 Task의 범위를
바꾸는 경우가 잦기 때문이다. Task 목록의 정본은 이 plan이다.

**worker에게 한 번에 Task 1개만 준다.** "다음 것도 이어서 해라"라고 지시하지 않는다.
한 Task가 승인되어 머지된 뒤 다음 이슈를 만든다.

### Task 1건의 진행 순서

```text
1. 이슈 발행        gh issue create --title "[FEAT] ..." --label feature --assignee @me
2. 브랜치 생성      이슈의 Development > Create a branch (gh issue develop, main 기준)
3. 구현             Codex worker — 전용 worktree에서 해당 브랜치 체크아웃
4. 동료 리뷰        별도 Codex worker — 구현자의 추론 맥락 없이 diff만 보고 적대적 리뷰
5. 종합·판정        코디네이터가 구현 결과 + 리뷰 결과를 대조해 승인 또는 수정 재투입
6. PR               Closes #<이슈번호>, 팀원 1명 이상 Approve 후 Squash Merge
```

### 동료 리뷰 규칙 (4단계)

**리뷰어는 구현자와 다른 worker여야 하고, 구현자의 사고 과정을 보지 않는다.** 구현한
에이전트가 자기 코드를 리뷰하면 같은 전제 위에서 같은 결론에 도달한다 — 놓친 것을 다시
놓친다. 리뷰어에게는 다음만 준다.

- 해당 이슈 본문과 이 plan의 Task 항목 (= 무엇을 했어야 하는가)
- `git diff`와 테스트 실행 결과 (= 무엇을 했는가)

리뷰어에게 주지 않는 것: 구현자의 worker 로그, 구현 중 판단 근거, "왜 이렇게 했는지"에
대한 설명. 리뷰어는 **결과물만 보고 독립적으로 판단**해야 한다.

리뷰어의 임무는 승인이 아니라 **반증**이다. 다음을 우선순위대로 확인한다.

1. 봉인 규칙 위반 — Judge 경로가 candidate workspace를 참조하는가
2. 라벨 누출 — 정답이 candidate나 피드백 payload로 흘러가는가
3. Task 범위를 벗어난 변경 — 요청하지 않은 리팩터링이 섞였는가
4. 계약 위반 — 이 plan의 결정(D1~D7)과 어긋나는가
5. 테스트가 실제 실패를 잡는가 — 통과하는 테스트가 무엇을 보장하는지

리뷰어와 구현자의 판단이 갈리면 코디네이터가 코드를 직접 열어 판정한다. **다수결로 정하지
않는다.**

---

## 판정 규칙 (D5 상세)

spec 7장은 "단일 scalar leaderboard만 최적화하지 않는다"고만 하고 얼마나 좋아져야
promote인지 정하지 않는다. 고정 %를 쓰지 않는 이유는 **그 값이 잡음보다 큰지 알 수 없기
때문**이다. 같은 코드·같은 데이터로 seed만 바꿔도 `NDCG@10`은 흔들리며, 자율 루프가 수십
trial을 돌리면 **우연히 좋아 보이는 결과가 반드시 나온다.** 임계값이 잡음보다 낮으면
harness는 잡음을 champion으로 승격시키고 그 위에서 다음 실험을 이어가므로, 오류가 조용히
누적된다.

**σ 측정.** baseline(현행 champion)을 **seed만 바꿔 5회** 실행해 `NDCG@10`의 표준편차 σ를
구한다. 이 값이 "아무것도 바꾸지 않아도 흔들리는 폭"이다. σ는 ledger에 기록하고, 데이터
규모나 모델 구조가 크게 바뀌면 재측정한다.

**판정.**

| 판정 | 조건 |
| --- | --- |
| `promote` | `NDCG@10` 평균 델타 **≥ 2σ** **그리고** 모든 guardrail 델타 **≥ -1σ** |
| `revise` | 델타 ≥ 2σ 이지만 guardrail 위반 (아이디어는 유효하나 부작용이 있음) |
| `discard` | 그 외 |

**임계값 근거.** 정규분포 가정에서 우연히 2σ를 넘을 확률은 단측 약 2.3%로, trial 30회 기준
우연한 승격 기대값이 1건 미만이다. 1σ(약 16%)는 5회에 1회꼴로 잡음이 승격되어 쓸 수 없고,
3σ는 MVP에서 아무것도 승격되지 않아 **promote 경로 자체가 검증되지 않을** 위험이 있다.
guardrail을 -1σ로 더 민감하게 두는 것은 의도적 비대칭이다 — guardrail의 목적은 개선 측정이
아니라 악화 감지이고, 부작용을 놓치는 비용이 잘못 잡는 비용보다 크다.

**통계적 유의성 검정은 MVP 범위 밖이다.** seed 5회는 σ 추정에는 쓰되 가설검정에는 부족하다.

**promote 경로 검증 의무.** MVP 종료 전 **일부러 더 나은 candidate**(예: 유효한 피처 1개
추가)로 promote가 실제로 발생하는지 1회 확인한다. 이를 하지 않으면 "한 번도 promote되지
않은" 상태가 임계값 때문인지 코드 결함 때문인지 구별할 수 없다.

---

## 범위 밖

- 논문 자동 발견, PaperCard, Capability Matcher (spec 15장 6·7번 — 후속)
- 최종 `research-report.html` 전체 9개 섹션 (spec 10장 — 후속)
- `applications/experiment_platform/executor/verifier.py` 삭제 (별도 이슈)
- KubernetesJobRunner 연결 (spec 15장 10번)
- `training_entity` SQL 변경 — **불필요하다.** Judge는 slate 식별자와 라벨만 필요하고
  피처는 candidate가 스스로 조립하므로, 학습 데이터 계약을 건드리지 않는다.

---

## File Structure

| 새 위치 | 책임 |
| --- | --- |
| `autoresearch/research_harness/__init__.py` | 공개 타입 재수출 |
| `autoresearch/research_harness/slate.py` | 평가 slate 조립·라벨 봉인·fingerprint |
| `autoresearch/research_harness/judge.py` | 예측 점수 채점, 리랭킹 지표, Decision 산출 |
| `autoresearch/research_harness/ranking_metrics.py` | NDCG@k, Recall@k 순수 계산 |
| `autoresearch/research_harness/workspace.py` | disposable worktree 생성·주입·회수 |
| `autoresearch/research_harness/ledger.py` | Trial Ledger(JSONL) append·재개 |
| `autoresearch/research_harness/feedback.py` | 자가 피드백 payload 조립 |
| `autoresearch/research_harness/runner.py` | LocalRunner — candidate 실행 |
| `autoresearch/research_harness/controller.py` | 예산·반복 루프·checkpoint |
| `tests/research_harness/` | 위 각 모듈의 테스트 |

---

## Task 0: spec에 확정 결정 반영 후 커밋

현재 대상 spec은 **untracked 상태**이고 D1~D4가 반영되어 있지 않다.

- [ ] spec 4.3에 **artifact 정의** 추가 — "재평가 대상은 candidate가 산출한 예측 점수
      파일이며, Judge는 candidate 코드를 실행하지 않는다"
- [ ] spec 4.2에 **연구 공간 제한과 안전 제한의 분리** 명시 — allowlist는 폐기하되
      시크릿 커밋 차단은 유지
- [ ] spec 11장 MVP 범위에 verifier 대체를 명시적으로 포함
- [ ] spec 15장에 실행 위치 결정(D2) 반영
- [ ] D5(σ 기반 판정 규칙)를 spec 7장 `compare()` 설명에 반영 — spec은 현재 승격 기준을
      정의하지 않는다
- [ ] D6(`slate_id` 생성 시점 부여)를 spec 8장 `EvaluationSlateItem`에 반영
- [ ] 이슈 발행 → 그 이슈의 `Create a branch`로 브랜치 생성 → spec + 이 plan 커밋

**검증:** `git status`에 untracked spec/plan이 남지 않는다.

---

## Task 1-0: action log 생성 단계에 `slate_id` 부여 (D6)

Task 1의 선행 작업이다. **이 저장소의 데이터 계약을 바꾸는 유일한 작업**이므로 범위를
좁게 유지한다.

- [ ] `autoresearch/action_log_generation/schema.py`에 `slate_id`를 **optional 컬럼**으로
      추가 (하위 호환 유지 — 기존 파티션은 null)
- [ ] 유저별 후보 묶음을 만드는 지점에서 slate 단위로 ID를 부여한다. `daily.py`가
      `history_days=1`, `max_events_per_user_per_day=candidates_per_user`로 하루치 묶음을
      만드는 경로가 시작점이다
- [ ] ID 형식은 기존 `event_id` 규약(`{prefix}_{YYYYMMDD}_{seq:08d}`)과 충돌하지 않게 정한다
- [ ] **`docs/specs/2026-07-24-action-log-slice-semantics.md`의 파티션 계약에 영향이 없는지
      먼저 확인한다.** `dt=D`가 KST 하루치 서로소 슬라이스라는 계약과 slate 경계가
      충돌하면 spec 갱신을 먼저 제안한다
- [ ] 과거 파티션(`slate_id` null)은 **평가 대상에서 제외**한다는 규칙을 slate 빌더가
      강제하도록 Task 1에 전달. fallback 추론을 넣지 않는다
- [ ] 테스트: 같은 노출 묶음의 행이 같은 `slate_id`를 갖고 다른 묶음과 겹치지 않음,
      기존 파티션 읽기가 깨지지 않음

**검증:** `uv run python -m pytest tests/action_log_generation/ -v`

---

## Task 1: `EvaluationSlateSnapshot` 빌더

action log parquet에서 평가 slate를 조립하고 정답을 분리 봉인한다.

- [ ] `slate.py` 작성. 입력은 action log parquet 경로(로컬/GCS)와 대상 날짜 범위
- [ ] impression 행에서 slate 조립: `slate_id`(**Task 1-0의 원천 컬럼을 그대로 사용.
      추론하지 않는다**), `user_id`, `video_id`, `event_timestamp`,
      optional `original_rank`(원천 `rank`), optional `candidate_source`(원천 `exposure_source`)
- [ ] `slate_id`가 null인 행은 **오류로 거부**한다. 조용히 건너뛰거나 추론으로 채우지 않는다 (D6)
- [ ] 개발·검증용 입력은 `RuleBasedActionLogGenerator`로 로컬 생성한다 (D7 — LLM·API 키 불필요)
- [ ] click 귀속으로 `clicked` 산출. **귀속 규칙(30분 윈도우)은
      `autoresearch/jobs/feature_store_build.py`의 기존 SQL과 동일해야 한다** — 상수를
      공유하거나, 불가능하면 동일 규칙임을 테스트로 고정한다
- [ ] 산출물 3개를 content-addressed 디렉터리에 write-once 게시:
      - `slate.parquet` — **라벨 없음.** candidate에게 주입되는 파일
      - `labels.parquet` — `slate_id`, `video_id`, `clicked`. **봉인**
      - `manifest.json` — `evaluation_id`(전체 content hash), 행 수, 기간, 원천 파티션,
        slate 수, slate당 평균 크기, click 보유 slate 비율
- [ ] optional 컬럼의 실제 non-null 비율을 manifest에 기록 (갭 조사에서 미확인 항목)
- [ ] 테스트: 라벨 파일과 slate 파일의 컬럼 집합이 겹치지 않음, 동일 입력 →
      동일 `evaluation_id`, write-once 위반 시 실패

**검증:** `uv run python -m pytest tests/research_harness/test_slate.py -v`

---

## Task 2a: 리랭킹 지표 순수 함수

의존이 없고 순수 계산이라 단독으로 완결된다. 이 Task만으로 PR 1개를 만든다.

- [ ] `ranking_metrics.py` — `ndcg_at_k()`, `recall_at_k()`. binary relevance
- [ ] **0-click slate 처리 규칙을 명시적으로 정한다** — ideal DCG가 0이라 NDCG가 정의되지
      않으므로 평균에서 제외하고, 제외 비율을 `coverage`로 함께 보고한다. 조용히 0점으로
      처리하면 지표가 데이터 구성에 따라 왜곡된다
- [ ] 동점 score의 tie-breaking 규칙을 고정한다 — 정하지 않으면 같은 모델이 실행마다 다른
      NDCG를 낸다
- [ ] 테스트: 완전 정답 순서 → 1.0, 역순 → 최소값, 0-click slate 제외 동작, 동점 처리,
      k보다 짧은 slate, 클릭 수 > k인 slate

**검증:** `uv run python -m pytest tests/research_harness/test_ranking_metrics.py -v`

---

## Task 2b: Sealed Judge

- [ ] `judge.py` — 입력은 봉인 라벨 + candidate `predictions.csv`
- [ ] **predictions 스키마 강제 검증**: 컬럼은 `slate_id, video_id, score`.
      slate와 정확히 1:1이어야 한다 — 누락 행, 중복 행, slate에 없는 행, NaN/Inf score는
      전부 `invalid_predictions`로 거부. 거부는 실행 실패이지 지표 0점이 아니다
- [ ] 지표 산출: primary `ndcg_at_10`, ranking guardrail `recall_at_10`·`ndcg_at_24`,
      probability guardrail은 `evaluate.py`의 기존 구현 재사용
- [ ] `compare()` — D5 규칙(`≥2σ` 개선 + guardrail `≥-1σ`)으로
      `promote | revise | discard` + `reason_code` 산출. **임계값을 코드에 상수로 박지 않고
      σ를 인자로 받는다.** 실제 σ 값 측정은 Task 7에서 한다 — 여기서는 σ를 주입받아 판정하는
      로직만 만든다
- [ ] 테스트: 스키마 위반 6종 각각 거부, champion 동률 시 판정,
      σ 경계값(2σ 직전/직후, guardrail -1σ 직전/직후) 판정 전환

**검증:** `uv run python -m pytest tests/research_harness/test_judge.py -v`

**봉인 검증(필수):** Judge 모듈이 candidate workspace 경로를 참조하지 않음을 테스트로
고정한다.

---

## Task 3: CandidateWorkspace + 산출물 계약

verifier 대체가 실제로 일어나는 지점이다.

- [ ] `workspace.py` — 기준 SHA에서 disposable git worktree 생성, 종료 시 회수
- [ ] `harness_in/slate.parquet` 주입. **봉인 자산(labels, ledger, judge 체크아웃)은
      worktree 바깥 경로에 두고 주입하지 않는다**
- [ ] `harness_out/` 생성. candidate가 여기에만 산출물을 쓴다
- [ ] **allowlist 검사 없음.** 대신 커밋 직전 시크릿 스캔만 수행(D4). 기존
      `applications/experiment_platform/executor/verifier.py`의 시크릿 탐지 로직 재사용
      가능 여부를 먼저 조사하고,
      재사용 가능하면 공용 함수로 추출한다 — 복제하지 않는다
- [ ] diff content fingerprint를 계산해 ledger에 넘긴다(차단용이 아니라 기록용)
- [ ] 테스트: worktree 격리, 봉인 경로가 worktree 안에서 보이지 않음, 시크릿 포함 diff 거부,
      `.parquet`/`pyproject.toml` 수정이 **허용**되는지(D3·D4 회귀 방지)

**검증:** `uv run python -m pytest tests/research_harness/test_workspace.py -v`

---

## Task 4: Trial Ledger + checkpoint

- [ ] `ledger.py` — `experiment-ledger.jsonl` append-only
- [ ] trial당 기록: `trial_id`, 기준/candidate SHA, diff fingerprint, `evaluation_id`,
      seed, 전체 지표, decision과 reason_code, 소요 시간, 실패 reason code, champion lineage
- [ ] 단계별 idempotent checkpoint — 프로세스 종료 후 마지막 완료 단계부터 재개.
      **Job 전체 재실행을 기본 재시도 단위로 쓰지 않는다**(spec 7.1)
- [ ] 테스트: 중단 후 재개가 완료 단계를 건너뜀, 같은 trial 중복 append 방지,
      손상된 마지막 줄 복구

**검증:** `uv run python -m pytest tests/research_harness/test_ledger.py -v`

---

## Task 5a: LocalRunner

- [ ] `runner.py` — workspace에서 고정 진입점(`harness-predict`)을 subprocess로 실행.
      timeout·자원 상한·stdout/stderr tail 수집. 실패는 reason code로 분류
      (`predict_timeout`, `predict_crash`, `invalid_predictions`, …)
- [ ] 테스트: 정상 실행, timeout, 비정상 종료, 산출물 미생성 각각의 reason code

**검증:** `uv run python -m pytest tests/research_harness/test_runner.py -v`

---

## Task 5b: 자가 피드백 + Controller

- [ ] `feedback.py` — **에이전트에게 돌려주는 payload**:
      - primary/guardrail 지표 값과 champion 대비 델타
      - `decision`과 `reason_code`
      - 이전 trial 이력 요약 (무엇을 시도했고 왜 기각됐는지)
      - 실패 시 stage + reason code + 로그 tail
      - **행 단위 정답과 지표 구현 코드는 포함하지 않는다**
- [ ] `controller.py` — 예산(최대 시간/trial 수) 안에서 반복. spec 7장 루프 구조를 따르되
      MVP는 `hypothesis` 입력을 사람이 준 문자열로 받는다(논문 발견은 범위 밖)
- [ ] 실패 시 사용자에게 묻지 않고 다음 행동을 스스로 정한다(spec 7.1)
- [ ] 테스트: fake candidate(고정 점수 산출)로 2 trial 이상 루프가 도는지,
      피드백 payload에 라벨이 없는지, 예산 소진 시 정상 종료하는지

**검증:** `uv run python -m pytest tests/research_harness/ -v`

---

## Task 6: CLI 진입점 + 문서 갱신

- [ ] `autoresearch/cli.py`에 `harness-predict`(candidate용)와 `harness-run`(연구 실행) 추가
- [ ] `harness-predict` 기본 구현 — 현행 champion 모델로 slate를 점수화한다.
      **이것이 baseline이자 candidate가 고쳐 나갈 출발점이다**
- [ ] `README.md`와 `.claude/docs/agent-project-reference.md`에
      `autoresearch/research_harness/` 추가 (CLAUDE.md 필수 규칙)
- [ ] `docs/README.md` 역할별 인덱스에 이 spec/plan 등재

**검증:**
```bash
uv run python -m pytest -v
uv run --no-sync ruff check applications autoresearch tests tools
```

---

## Task 7: baseline σ 측정 + end-to-end 완주

앞선 Task가 전부 머지된 뒤에만 가능하다. σ 측정에 `harness-predict`(Task 6)와
slate(Task 1)가 모두 필요하기 때문이다.

- [ ] **baseline σ 측정** (D5) — 현행 champion을 seed 5개로 실행해 `NDCG@10` 표준편차를
      구하고 ledger에 기록한다. 측정 전에는 `compare()`가 판정할 수 없다
- [ ] 측정된 σ와 baseline 지표 절대값을 이 plan과 spec에 기록한다 — 이후 실험의 기준선이다
- [ ] 로컬 end-to-end 1회 완주 — slate 조립 → baseline 점수화 → Judge 판정 → ledger 기록
      → 피드백 반환 → 2차 trial
- [ ] **promote 경로 검증** — 일부러 개선된 candidate(유효한 피처 1개 추가)로 `promote`가
      실제 발생하는지 확인한다. 이걸 하지 않으면 승격이 없는 상태의 원인을 알 수 없다
- [ ] 중단 후 checkpoint 재개 1회 확인

**검증:** 전체 테스트 + 실제 1회 완주 로그와 ledger 산출물

---

## 완료 조건

- [ ] 에이전트가 저장소 어느 파일이든 수정해도 harness가 차단하지 않는다
- [ ] candidate가 evaluator·테스트·split 코드를 고쳐도 판정 수치가 바뀌지 않는다
- [ ] `predictions.csv` 계약 위반이 지표 조작이 아니라 실행 실패로 처리된다
- [ ] 판정 결과가 구조화된 피드백으로 에이전트에게 돌아가고, 다음 trial이 그것을 참조한다
- [ ] 프로세스를 중단한 뒤 마지막 checkpoint부터 재개된다
- [ ] 로컬에서 Kubernetes 없이 완주한다
- [ ] baseline σ가 측정되어 ledger에 기록되고, 판정이 그 값을 입력으로 쓴다
- [ ] 일부러 개선된 candidate로 `promote`가 실제 발생함을 1회 확인했다 (D5 검증 의무)
