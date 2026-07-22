# 일일 폐루프 완성 — action-log CLI의 모델 노출 배선과 cutover

- 날짜: 2026-07-22
- 이슈: #222
- 선행: #221(모델 노출 조립 provider, PR #225 머지됨)
- 관련: docs/specs/2026-07-22-model-exposure-assembly.md (provider·태그 계약),
  docs/specs/2026-07-13-public-batch-execution-contract.md (공개 CLI 계약 — 본
  spec이 갱신)

## 배경

#221이 `user_recommendations` 파티션을 읽어 24개(모델 17·트렌딩 5·랜덤 2)
노출을 조립하는 `ModelExposureRound`(provider + 태그 맵)를 완성했다. 남은
것은 일일 action-log 생성이 이 provider를 실제로 사용하고, 노출 태그가
최종 event log까지 운반되는 배선이다. 완성 시 폐루프(champion 모델 → 노출
→ LLM 클릭 → action log → 재학습)가 닫힌다.

일일 생성은 3개 모드로 돈다: `single`(로컬 일괄), `shard`(LLM 병렬 분할),
`merge`(유저별 최고 1개 + 관련성 커트라인 판정 + 확장). 운영은 shard+merge다.

## 목표

1. `python -m autoresearch.jobs.action_log`의 single·shard 모드가 모델 노출
   provider를 **기본 사용**하도록 전환 (`--exposure-source model|heuristic`,
   기본 `model`; 휴리스틱은 명시적 폴백으로 보존)
2. 노출 태그(exposure_source·rank·ctr_score·policy_version)가 draft를 타고
   shard → merge를 건너 최종 event log 레코드에 조인되는 운반 계약
3. 공개 배치 계약 문서 갱신과 Airflow 인계 노트

## 비범위

- LLM 판정·클릭 선정 로직 변경 — 없음 (유저별 최고 1개 + 관련성 커트라인
  `click_threshold` 방식 유지, 이슈 #222 본문의 라벨 방식 결정 준수; CTR은
  강제 목표가 아니라 창발 지표)
- exposure_source별 CTR 집계 리포트(온라인 매트릭 비교) — 후속 이슈
- Airflow DAG 편성(순서·재시도) — `Autoresearch-airflow` 소유. 본 spec은
  인계 요구사항만 명시한다.

## 핵심 설계 결정

### 결정 1 — 태그는 draft에 실어 운반한다 (merge 재조립 금지)

shard 모드는 draft parquet만 남기고 merge는 그것만 읽는다. 태그를 merge
시점에 재조립(BQ 재조회 + 결정적 재실행)하는 대안은 shard와 merge의 인자
불일치(ratio·seed drift)가 **조용히 잘못된 태그**를 만들 수 있어 배제한다.
대신 `ImpressionDraft`에 태그 필드 4개를 additive optional로 추가해 draft
parquet이 태그를 운반한다:

| 필드 | 타입 | 값 |
| --- | --- | --- |
| `exposure_source` | `Literal["model","trending","random"] \| None` | 슬롯 출처 |
| `exposure_rank` | `int \| None` (≥1) | ExposureMetadata.rank |
| `exposure_ctr_score` | `float \| None` | model 슬롯만, 그 외 None |
| `policy_version` | `str \| None` | model_run_id (계보) |

`ACTION_LOG_DRAFT_PARQUET_SCHEMA`(와 이를 spread하는 checkpoint 스키마)에
같은 4컬럼을 추가한다. 구 shard draft(컬럼 부재)는 pydantic 기본값(None)으로
읽혀 하위 호환이며, 체크포인트 재개 시에도 provider가 전 유저에 대해
재호출되므로(#221 검증: work 목록 구성 시 후보 조립 선행) 태그 맵은 항상
완전하다.

- `attach_exposure_tags(drafts, metadata) -> list[ImpressionDraft]`: 생성
  직후 태그를 draft에 심는다 (single: expand 전 / shard: draft parquet 쓰기 전).
- `_expand_events`는 외부 metadata 인자가 없으면 **draft에 실린 태그로부터
  ExposureMetadata 맵을 복원**해 사용한다(fallback). merge는 이 fallback
  덕에 변경이 필요 없다. 복원 규칙: `policy="model"`,
  `is_exploration = (exposure_source == "random")`, 나머지는 필드 그대로.
- 기존 외부 metadata 인자 경로(simulate_policy_round)는 우선순위 유지 —
  동작 불변.

### 결정 2 — provider 구성은 jobs CLI, 주입은 factory seam

`autoresearch/action_logs/`는 BQ 비의존 순수를 유지한다. BQ 접근이 필요한
provider 구성은 공개 CLI(`autoresearch/jobs/action_log.py`)가 담당하되,
`src.pipeline.model_exposure_provider`를 **model 모드에서만 지연 import**
한다(heuristic 모드는 src·BQ 무의존 유지). 의존 방향
`jobs → src.pipeline → action_logs.pipeline`은 비순환이다(저장소 내
`autoresearch → src` import 선례는 없으나, Dockerfile.app이 두 패키지를
모두 포함하고 daily 추천 배치가 이미 역방향 조합을 실증했다 — 계층 원칙은
"action_logs 순수 유지"이며 jobs는 인프라 조립 레이어다).

videos는 daily 함수가 파티션 parquet에서 로드하므로, CLI는 provider를
직접 만들 수 없다. seam은 **factory**로 한다:

```python
# daily.run_daily_action_log / run_daily_action_log_shard 신규 파라미터
candidate_provider_factory: Callable[
    [list[dict]],  # 로드된 videos
    tuple[CandidateProvider, Mapping[tuple[str, str], ExposureMetadata]],
] | None = None
```

daily는 videos 로드 후 factory를 1회 호출해 (provider, 태그 맵)을 얻고,
provider를 기존 `candidate_provider` seam에, 태그 맵을 attach 단계에
넘긴다. None이면 기존 휴리스틱 경로 그대로다(하위 호환 — 기존 Python 호출
불변).

### 결정 3 — cutover는 기본값 전환 + 문서화된 명시 폴백

`--exposure-source`의 기본값은 `model`이다(이슈 합의: "기본 사용 전환").
이는 이미지 업그레이드만으로 동작이 바뀌는 계약 변경이므로:

- 공개 배치 계약 문서에 신규 인자·기본값·BQ 의존·실패 모드를 등재한다.
- **Airflow 인계 요구사항**: action_log DAG는 daily_recommendations 성공에
  의존해야 하며(같은 dt), 전환 시점은 Autoresearch-airflow가 인자
  `--exposure-source`를 명시 전달해 제어할 수 있다.
- 파티션 지연 정책: model 모드에서 해당 dt의 `user_recommendations`가
  비면 **exit 1 (fail-fast)** — 조용한 휴리스틱 대체 금지. 운영 폴백은
  `--exposure-source heuristic` 재실행으로 명시적으로만 수행한다
  (#221 spec의 미해결 항목 확정).

## CLI 계약 변경 (`autoresearch/jobs/action_log.py`)

| 인자 | 모드 | 규칙 |
| --- | --- | --- |
| `--exposure-source {model,heuristic}` | single·shard | 기본 `model`. merge는 거부(_reject — 태그는 draft가 운반) |
| `--recommendations-table <bare name>` | single·shard의 model 모드만 | 기본 env `CTR_TRAINING_BQ_RECOMMENDATIONS_TABLE`(→`user_recommendations`). heuristic 모드는 거부 |

model 모드 테이블 해석은 daily 추천 배치와 동일 체계:
`{BIGQUERY_PROJECT}.{BIGQUERY_DATASET}.{table}` (상수는
`src.pipeline.build_training_dataset`에서 지연 import — #216 리뷰의 기본값
중복 지적 재발 방지). rankings의 dt는 `--partition-date`와 동일해야 한다
(dt 정합 — 노출·추천·트렌딩이 한 dt).

## 인계 확인 항목 처리 (#221 → #222)

- **rank 병독 규칙 downstream 확인 — 완료**: 학습 경로(`derive_wide_events`)
  의 wide 스키마는 rank를 소비하지 않고(event_id/user_id/video_id/
  timestamp/clicked/liked/watch_time_sec), 품질잡은 model_validate만
  수행한다. 현재 rank를 단독 소비하는 downstream은 없다. by-rank 집계를
  도입하는 시점에 `exposure_source='model'` 필터 전제를 적용한다(PR #225
  리뷰 합의).
- **파티션 지연 운영 정책 — 결정 3에서 확정** (fail-fast + 명시 폴백).
- **daily legacy 스키마 관용 — #221에서 선반영 완료.**
- **`_expand_events` 조인 — 결정 1의 draft-fallback으로 해소** (공개
  래퍼 `expand_action_log_drafts`를 그대로 쓰면 된다).

## 에러 처리

- model 모드: rankings 파티션 0행·트렌딩 0건 → 시작 시점 RuntimeError
  (exit 1). BQ 조회 실패도 동일.
- 태그 없는 draft(휴리스틱 라운드·구 shard)는 오류가 아니다 — 무태그
  이벤트로 확장된다(기존과 동일).
- 유저 단위 격리·quarantine 경로는 변경 없다.

## 테스트 계획

1. draft 태그 왕복: attach → parquet 쓰기/읽기 → 복원 맵 동등, 구 스키마
   draft(컬럼 부재) 하위 호환
2. `_expand_events` draft-fallback: 태그 실린 draft → 이벤트에
   exposure_source·rank·ctr_score·policy_version 조인, 외부 metadata 우선
3. single 흐름: factory 주입 → 최종 이벤트 태그 (fake generator)
4. shard→merge 흐름: shard가 태그 실은 draft parquet을 쓰고 merge가 읽어
   이벤트 태그 (tmp_path e2e)
5. CLI: 기본 model, `--exposure-source heuristic` 회귀, merge에서
   `--exposure-source`/`--recommendations-table` 거부, model 모드 factory
   배선(모킹)
6. 계약 문서 단언(release workflow 테스트 스타일)이 있으면 갱신

## 후속

- exposure_source별 CTR 집계 리포트(온라인 매트릭) — 별도 이슈
- 두 번째 additive 컬럼 도입 시 daily 재검증을 집합 차집합 방식으로 전환
  (#221 spec 설계 노트)
- Airflow: action_log DAG의 daily_recommendations 선행 의존 추가
  (Autoresearch-airflow 이슈로 인계)
