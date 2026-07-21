# 정책 시뮬레이션 라운드 (Policy Simulation Round)

## 목표

학습된 CTR 모델(Reranker)이 노출을 직접 선정했을 때, 기존 키워드 휴리스틱 대비
클릭 성과가 개선되는지를 가상 유저 시뮬레이션으로 측정한다. 산출되는 event log는
폐루프 재학습에 그대로 재사용 가능한 형태(정책·점수·exploration 메타데이터 포함)로
남긴다.

## 배경

현재 파이프라인의 노출 선정은 `autoresearch/action_logs/candidate.py`의 키워드
substring 휴리스틱이 수행하고, 모델 평가는 그 노출 분포 위의 ROC-AUC
(`src/pipeline/evaluate.py`)뿐이다. 이는 "이미 노출된 후보 중 클릭 판별"만
검증하며, 모델의 실제 임무인 "후보 pool에서 무엇을 노출할지 선택"은 검증된 적이
없다. 모델이 새로 고르는 노출에는 기존 로그에 클릭 라벨이 없으므로, LLM 페르소나
판정을 다시 거쳐 라벨을 생성해야 한다.

또한 향후 폐루프 재학습(모델 노출 로그로 다음 세대 학습)을 시작하려면 노출 확률
관련 메타데이터(`ctr_score`, `is_exploration`, `policy_version`)가 로그 스키마에
처음부터 존재해야 한다. 사후 추가 시 이전 라운드 로그는 재학습에 사용할 수 없다.

## 핵심 설계 결정

1. **배치 직접 로드 (serving HTTP 미경유)** — 배치 프로세스가
   `src/serving/model_loader.load_reranker()`와 `src/serving/service.Reranker`를
   라이브러리로 import해 점수를 계산한다. 서빙과 동일 코드 경로이므로 점수 불일치
   위험이 없고, 서버 수명 관리·네트워크 실패 모드를 배치에 들이지 않는다. HTTP
   경유 리허설은 비범위(후속 과제)로 남긴다.
2. **paired 2-정책 비교** — 같은 유저·같은 영상 pool에 대해 두 정책을 병행한다.
   - `baseline` 정책: 기존 `build_candidates()` 휴리스틱
   - `model` 정책: 피처 조립 → Reranker 점수 → Top-K + exploration
   유저를 반씩 나누는 방식 대비 유저 표본 노이즈가 제거된다(가상 유저이므로 정책
   간 간섭 효과가 없다).
3. **합집합 판정** — LLM 판정은 (유저, 영상) 쌍당 1회만 수행한다. 유저별로 두
   정책 노출의 합집합을 판정하고 결과를 양 정책이 공유한다. 비용을 겹침만큼
   절감하며, 같은 쌍에 대한 LLM 비결정성이 정책 간 비교 노이즈로 들어오는 것을
   구조적으로 차단한다. 콜당 context는 기존 chunk_size 분할이 그대로 적용되어
   늘어나지 않는다(총 토큰만 최대 ~2배).
4. **합동 CTR 정규화** — 두 정책의 `ImpressionDraft`를 한 pool로 합쳐 기존
   `_clicked_indices()` 전역 2% 정규화를 1회만 적용한다. 정책별로 따로 정규화하면
   양쪽 CTR이 강제로 target_ctr이 되어 비교가 무의미해진다. 합동 정규화는 클릭
   예산이 `click_propensity` 높은 노출로 쏠리게 하여 정책 간 상대 비교 신호를
   보존하면서 전역 CTR 계약(≈2%)도 유지한다.
5. **exploration 슬롯 포함** — model 정책 노출의 `exploration_ratio`(ε) 비율은
   비-Top-K 후보에서 균등 랜덤 선정하고 `is_exploration=true`로 기록한다. 폐루프
   재학습 시 피드백 편향(모델이 좋아하는 것만 노출→학습→취향 붕괴)을 완화하는
   장치이며, 첫 라운드부터 스키마에 포함해야 이후 로그가 재학습에 유효하다.

## 컴포넌트

### 신규

1. **피처 조립 공용 함수** — `(유저, 영상, 기준 시점) → 15개 피처 row`.
   `src/pipeline/build_training_dataset.py`의 `derive_wide_events()`에서 해당
   로직을 추출해 학습 데이터셋 빌더와 시뮬레이션 라운드가 같은 코드를 사용한다.
   별도 재구현은 그 자체가 학습-서빙 스큐이므로 금지한다. 추출은 구조 변경이므로
   동작 변경과 커밋을 분리한다.
2. **정책 선택기** — `select_exposures(scored_candidates, k, exploration_ratio,
   rng)`. Reranker 점수 내림차순 상위 `round(k×(1−ε))`개(exploitation) + 나머지
   후보 균등 랜덤 `k−exploitation`개(exploration). 각 노출에 `rank`(1-base,
   점수순), `ctr_score`, `is_exploration`을 태깅한다. seed 고정 시 결정론을
   보장한다. `k ≥ 후보 수`이면 전 후보 노출(exploration 슬롯 없음).
3. **배치 진입점** — `src/pipeline/simulate_policy_round.py`. 흐름:
   virtual_users·영상 pool 로드 → 두 정책 노출 산출 → 유저별 합집합 → LLM 판정
   (기존 chunking·격리 기계 재사용) → 합동 정규화 → `_expand_events` → parquet
   저장 → 평가 리포트 출력. Reranker는 `load_reranker()`로 로드하며 local/mlflow
   소스를 모두 지원한다. `policy_version`은 MLflow run_id(local 소스이면 CLI
   인자로 명시)로 기록한다.

### 수정

4. **`autoresearch/action_logs/pipeline.py` — 후보 주입 seam** — 현재 내부에서
   `build_candidates()`를 호출하는 경로에, 미리 계산된 유저별 후보 목록을 주입
   받는 입구를 연다(callable 또는 후보 목록 인자). 의존 방향은 `src` →
   `autoresearch` 단방향을 유지하며(`autoresearch`는 `src`를 import하지 않는다),
   LLM 판정·chunking·격리·정규화 기계는 수정 없이 재사용한다.
5. **`autoresearch/action_logs/schema.py` — EventLog additive 확장** — 기존
   `source: Literal["historical", "online_simulated"]`의 `online_simulated` 값을
   이번에 처음 실사용한다. 신규 필드는 전부 optional(기본 `None`)로 기존
   historical 로그와 하위 호환을 유지한다.

   | 필드 | 타입 | 의미 |
   | --- | --- | --- |
   | `policy` | `Literal["baseline", "model"] \| None` | 이 노출을 선정한 정책 |
   | `ctr_score` | `float \| None` | 노출 시점 Reranker 점수 (baseline은 `None`) |
   | `is_exploration` | `bool \| None` | exploration 슬롯 여부 (model 정책만 non-null) |
   | `policy_version` | `str \| None` | 모델 식별자 (MLflow run_id) |

   `rank`는 기존 필드를 재사용한다. `to_warehouse_row()`·parquet 스키마를 함께
   확장한다. 라운드 메타(`k`, `exploration_ratio`, `policy_version`)는 리포트
   JSON과 `EventLogBatch.request`에 기록한다(shard manifest는 이 배치가
   사용하지 않으므로 확장하지 않는다).

## 실행 파라미터 기본값

CLI 인자로 노출하되 기본값은 다음과 같다.

| 인자 | 기본값 | 비고 |
| --- | --- | --- |
| `k` (정책당 유저별 노출 수) | 10 | 두 정책 동일 값 사용 (공정 비교) |
| `exploration_ratio` | 0.1 | model 정책에만 적용 |
| `target_ctr` | 0.02 | 기존 라운드와 동일 (합동 pool 기준 1회) |
| `max_users` | 무제한 (전체 유저) | CLI로 상한 지정 가능. 첫 라운드는 100~200명 권장 (LLM 비용 통제) |

## 평가 리포트

라운드 종료 시 stdout + JSON 파일 + **자기완결 HTML 리포트**(`policy_round_report.html`, 외부 의존성 없는 인라인 차트)로 남기고, 핵심 수치는 MLflow run으로도 기록한다(학습 run과 동일 experiment 관례).

| 지표 | 의미 |
| --- | --- |
| 정책별 CTR | 합동 정규화 후 클릭/노출 — 헤드라인 지표 |
| 정책별 평균 `click_propensity` | 정규화 전 raw 신호 — 정규화 방식에 독립적인 보조 지표 |
| 노출 겹침률 (Jaccard) | 두 정책이 같은 영상을 고른 정도. 과도하게 높으면 비교 무의미 경고 |
| exploration 슬롯 CTR | exploitation 대비 exploration 성과 — ε 조정 근거 |
| unseen category 건수 | `rerank_with_diagnostics()` 진단 수집 — 스코어링 품질 경고 |

## 에러 처리

기존 파이프라인의 실패 격리 철학을 따른다.

- **유저 단위 격리**: 한 유저의 피처 조립·스코어링 실패는 해당 유저만 quarantine
  하고 배치는 계속한다. 격리 비율이 임계치를 넘으면 기존
  `ActionLogGenerationError` 패턴대로 전체 실패 처리한다.
- **fail-fast**: Reranker 아티팩트 로드 실패(`ModelArtifactError` 등)와 피처
  컬럼 계약 위반(`MissingFeatureColumnsError`)은 라운드 전체가 무효이므로 시작
  즉시 중단한다.
- **LLM 판정 실패**: 기존 chunk 단위 격리·quarantine을 그대로 사용한다.

## 테스트

`tests/` 플랫 구조 관례를 따른다.

1. 정책 선택기 단위 테스트: Top-K 정렬 정확성, ε 슬롯 수, seed 결정론,
   `k ≥ 후보 수` 엣지.
2. 피처 조립 동일성 테스트: 추출된 공용 함수가 기존 학습셋 산출물과 동일 row를
   생성하는지 — 추출 리팩터링의 안전망으로 가장 중요.
3. 합동 정규화 비교 테스트: 가짜 draft로 propensity 높은 정책이 클릭을 더
   가져가는지 검증.
4. 스키마 하위 호환 테스트: 신규 optional 필드 없이 기존 historical 로그가
   그대로 검증을 통과하는지.
5. 엔드투엔드 스모크: 가짜 LLM generator(기존 테스트 fake 패턴 재사용) + 소형
   유저·영상 셋으로 배치 전체 실행 → 리포트 산출 확인.

LLM 실호출 테스트는 두지 않는다 — 판정 기계는 기존 테스트가 커버하며, 신규
코드는 전부 fake로 검증 가능하다.

## 비범위 (Non-goals)

- serving HTTP 경유 실행(점수 획득이 함수 하나로 고립되므로 후속에 교체 가능).
- 실제 재학습 실행 — 본 라운드는 재학습 가능한 로그 형태를 준비할 뿐, 재학습
  라운드는 별도 작업으로 다룬다.
- LLM 판정 자체의 현실성 개선(프롬프트·페르소나 품질)은 이 작업의 범위가 아니다.

## 한계

클릭 판정자가 LLM 페르소나이므로, 측정되는 개선은 시뮬레이션 세계 내부의
개선이다. 실제 사용자 CTR 개선의 증명이 아니며, 실서비스 전환 시 LLM 자리에
실사용자·A/B 테스트가 들어가는 구조의 리허설로 해석한다.
