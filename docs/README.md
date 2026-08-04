# 문서 인덱스

이 저장소의 문서는 아래 규칙으로 배치합니다.

| 위치 | 내용 | 수명 |
|---|---|---|
| `adr/` | 아키텍처 결정 기록 (ADR) | 영구 |
| `specs/` | 살아있는 계약·설계 spec (`YYYY-MM-DD-<slug>.md`) | 유효한 동안 |
| `plans/` | 진행 중 구현 계획 (`YYYY-MM-DD-<slug>.md`) | 구현 완료 시 archive로 |
| `guides/` | 운영·아키텍처 가이드 | 상시 갱신 |
| `runbooks/` | 운영 절차·트러블슈팅 기록 | 상시 갱신 |
| `reports/` | 팀 공유용 시각화 리포트 (HTML) | 참조되는 동안, 이후 archive/reports로 |
| `archive/` | 완료·과거 spec/plan/리포트 보존 | 영구 (수정하지 않음) |

새 spec/plan 작성 규칙은 [`CLAUDE.md`](../CLAUDE.md)의 *Spec / Plan First* 절을
따릅니다. spec/plan이 구현 완료되어 더 이상 계약으로 쓰이지 않으면
`archive/specs/`, `archive/plans/`로 옮깁니다. 코드 디렉토리 안에 문서를 두지
않습니다.

## 역할별 인덱스

문서는 유형(adr/specs/plans/guides)별로 배치되지만, 아래는 역할(도메인) 기준
모아본 것이다. 같은 문서가 여러 역할에 중복 등장할 수 있다.

### 📥 데이터 수집 (YouTube Collection)

- [ADR 0001 — YouTube 프록시의 목적](adr/0001-youtube-proxy-purpose.md)
- [Spec — GCS raw 데이터 BigQuery 적재](specs/2026-07-11-load-raw-to-bigquery.md)
- [가이드 — 데이터 레이크](guides/data-lake.md)

### 👤 가상 유저 (Virtual Users)

- (현재 전용 guide 없음 — `autoresearch/virtual_users/` 코드 및
  `tests/test_virtual_users_*.py` 참조)

### 📝 Action Log

- [가이드 — action_logs 모듈 사용법](guides/action-log.md)
- [가이드 — Agent Simulator 명세 (action log SSOT)](guides/agent-simulator-spec.md)

### 🎯 Feature Engineering

- [가이드 — 피처 스토어](guides/feature-store.md)
- [가이드 — Feast GCP 설정](guides/feast-gcp-setup.md)
- `feature_repo/` 디렉토리 (Feast 규격 — `feature_definitions.py`, `feature_store.yaml`)

### 🏋️ 학습 파이프라인 (Training)

- [가이드 — 학습 데이터셋](guides/training-dataset.md)
- [가이드 — CTR 모델 명세](guides/ctr-model-specification.md)
- [가이드 — 학습 실험 provenance 애플리케이션 설계](guides/training-experiment-provenance.md)
- [Spec — 모델 승격 구조화 결과 계약](specs/2026-07-29-model-promotion-structured-outcome.md)
- [Spec — CTR 모델 배포 패키지](archive/specs/2026-08-01-ctr-model-deployment-package.md) (구현 완료·아카이브)
- [Plan — CTR 모델 배포 패키지 구현](archive/plans/2026-08-01-ctr-model-deployment-package.md) (구현 완료·아카이브)
- [Spec — 학습 윈도우 spine 커버리지 가드](specs/2026-08-01-training-window-coverage-guard.md) — 기준값 근거·lineage 계약·가드의 한계 (#464)
- [Spec — paired offline 실험 배치·비교 결과 계약](specs/2026-08-03-paired-offline-experiment-comparison.md) — 조건 격리 좌표·피처 보존·결과 payload (#454)
- [Plan — paired offline 실험 배치·비교 결과 구현](plans/2026-08-03-paired-offline-experiment-comparison.md) (#454)
- [Spec — 모델 성능 열화 시점 측정(rolling-origin 평가)](specs/2026-08-03-model-degradation-rolling-origin-evaluation.md) — 단일 cutoff 기반 forward degradation evaluation, 날짜 구간·평가일 상태 계약, video staleness (#471)
- [Plan — 모델 성능 열화 시점 측정 구현](plans/2026-08-03-model-degradation-rolling-origin-evaluation.md) (#471)
- [Spec — 실험별 Feast Registry·offline 실행 격리](specs/2026-07-31-experiment-isolated-offline-run.md) (#454 실행 context)
- [Plan — `src/` → `autoresearch/` 패키지 통합](plans/2026-07-15-src-package-merge.md) (팀 합의 대기)
- `src/pipeline/`, `src/models/`, `src/features/` (CTR 학습·평가 코드)

### 🚀 서빙 (Serving)

- [Spec — YouTube 리랭킹 서빙 API](specs/2026-07-16-reranking-serving-api.md)
- [Spec — Rerank Serving 성능·비용·안정성 벤치마크](specs/2026-07-31-rerank-serving-performance-benchmark.md)
- [Plan — Rerank Serving 성능 벤치마크 구현](plans/2026-08-01-rerank-serving-performance.md)
- [Runbook — 리랭킹 서빙 부하측정 운영 절차](runbooks/rerank-loadtest.md)
- [Plan — Reranking Serving API 구현](archive/plans/2026-07-16-reranking-serving-api.md) (완료·아카이브)
- [시각화 — Serving Feature Build: 무엇이 바뀌었나](reports/2026-07-22-serving-feature-build-overview.html) — 비개발 팀원용 변경 흐름·운영 경계 안내
- `src/serving/` (FastAPI 추론 서버), `deploy/serving/` (이미지 정의)

### 🤖 오케스트레이션 (Experiment API)

- [Spec — Agent Orchestration 채팅 저장 스켈레톤](archive/specs/2026-07-30-agent-orchestration-chat-postgres-skeleton.md) (구현 완료)
- [Plan — Agent Orchestration 1단계 구현 계획](archive/plans/2026-07-30-agent-orchestration-chat-postgres-skeleton.md) (구현 완료)
- [Plan — Agent Orchestration PR 사전 병합 강화](archive/plans/2026-07-31-agent-orchestration-premerge-hardening.md) (구현 완료)
- [Spec — Agent Orchestration 실험 워크벤치 v0](archive/specs/2026-08-01-agent-orchestration-experiment-workbench-v0.md) (구현 완료)
- [Plan — Agent Orchestration 실험 워크벤치 v0](archive/plans/2026-08-01-agent-orchestration-experiment-workbench-v0.md) (구현 완료)
- [Spec — 실험 Step 추적 v0](archive/specs/2026-08-04-experiment-step-tracking-v0.md) (구현 완료) — 에이전트 진행 상황 실시간 관찰 계약
- [Plan — 실험 Step 추적 v0](archive/plans/2026-08-04-experiment-step-tracking-v0.md) (구현 완료)
- [Spec — Agent Orchestration `/chat` API 계약](specs/2026-08-01-agent-orchestration-chat-api-contract.md) — 내부 호출 서비스의 요청·응답·오류·저장 의미 정본
- `agent_orchestration/` (FastAPI + Codex CLI/OpenAI + PostgreSQL 실험 API)
- [Spec — Agent Orchestration GKE 내부 배포](specs/2026-07-30-agent-orchestration-gke-internal-deployment.md)
- [Plan — Agent Orchestration GKE 내부 배포](plans/2026-07-30-agent-orchestration-gke-internal-deployment.md)

### 🌬️ 오케스트레이션 (Airflow)

- [Spec — Autoresearch-airflow 경계 컷오버](specs/2026-07-13-autoresearch-airflow-boundary-cutover.md) (Phase 1~5 완료, Phase 6 대기)
- [Spec — 공개 batch 실행 계약 batch-contract-v1](specs/2026-07-13-public-batch-execution-contract.md)
- 본 저장소 `dags/`는 비어있으며 DAG는 [`Autoresearch-airflow`](https://github.com/SKYAHO/Autoresearch-airflow) 소유

### ☁️ 인프라 (Infrastructure)

- [Spec — MLflow 배포 전략](specs/2026-07-14-mlflow-deployment-strategy.md)
- [가이드 — 데이터 웨어하우스 (BigQuery)](guides/data-warehouse.md)
- `deploy/mlflow/`, `proxy/` (Cloud Run forwarder), `Dockerfile.app`

### 📚 저장소 메타 (Repository Meta)

- [ADR 0002 — 저장소 책임 경계](adr/0002-repository-responsibility-boundaries.md)
- [Spec — 저장소 구조 재정리](specs/2026-07-15-repo-restructure.md)
- [Spec — 머지된 PR 리포트 아카이브](specs/2026-07-26-pr-report-archive-design.md)

## ADR

- [0001 — YouTube 프록시의 목적](adr/0001-youtube-proxy-purpose.md)
- [0002 — 저장소 책임 경계](adr/0002-repository-responsibility-boundaries.md)

## 유효한 Spec (살아있는 계약)

- [공개 batch 실행 계약](specs/2026-07-13-public-batch-execution-contract.md) —
  Airflow가 소비하는 공개 CLI·인자 계약
- [Autoresearch-airflow 경계 컷오버](specs/2026-07-13-autoresearch-airflow-boundary-cutover.md)
- [MLflow 배포 전략](specs/2026-07-14-mlflow-deployment-strategy.md)
- [GCS raw 데이터 BigQuery 적재](specs/2026-07-11-load-raw-to-bigquery.md)
- [오프라인 feature build 배치](specs/2026-07-22-feature-store-build-batch.md)
- [저장소 구조 재정리](specs/2026-07-15-repo-restructure.md) — 이 문서 구조의 근거,
  `src/` 패키지 통합 목표 구조 포함
- [머지된 PR 리포트 아카이브](specs/2026-07-26-pr-report-archive-design.md) —
  GitHub Pages에 누적된 merge PR 리포트의 정적 검색 인덱스
- [모델 승격 구조화 결과 계약](specs/2026-07-29-model-promotion-structured-outcome.md) —
  승격·게이트 미달·후보 없음·실행 오류의 기계 판독 결과와 Airflow 인계 계약
- [paired offline 실험 배치·비교 결과 계약](specs/2026-08-03-paired-offline-experiment-comparison.md) —
  baseline/candidate 조건 격리, 실험 피처 보존, `comparison_passed`/`rejected`/`failed` 결과 payload
- [모델 성능 열화 시점 측정(rolling-origin 평가)](specs/2026-08-03-model-degradation-rolling-origin-evaluation.md) —
  단일 cutoff 학습 → 하루 단위 순차 평가로 ROC-AUC 열화 곡선·열화 지점 산출, 데이터 가용성 제약(`A-D`)
- [Rerank Serving 성능·비용·안정성 벤치마크](specs/2026-07-31-rerank-serving-performance-benchmark.md)
- [Agent Orchestration `/chat` API 계약](specs/2026-08-01-agent-orchestration-chat-api-contract.md) —
  내부 호출 서비스의 요청·응답·오류·저장 의미 정본

## 가이드

- [전체 파이프라인 개요](guides/pipeline-overview.md) — 배치·서빙·시뮬레이션 폐루프 mermaid 다이어그램
- [데이터 레이크](guides/data-lake.md) · [데이터 웨어하우스](guides/data-warehouse.md)
- [학습 데이터셋](guides/training-dataset.md)
- [피처 스토어](guides/feature-store.md) · [Feast GCP 설정](guides/feast-gcp-setup.md)
- [CTR 모델 명세](guides/ctr-model-specification.md)
- [학습 실험 provenance 애플리케이션 설계](guides/training-experiment-provenance.md)
- [Agent Simulator 명세 (action log SSOT)](guides/agent-simulator-spec.md)
- [action_logs 모듈 사용법](guides/action-log.md)
- [Release & 배포 파이프라인](guides/release-pipeline.md) — CI/CD·GAR push·digest 승격·GKE 배포 자동화
- [CTR 학습 이미지](guides/training-image.md) — `Dockerfile.train`, MLflow tracking URI 연동
- [YouTube 트렌딩 수집 파이프라인](guides/youtube-collection.md) — API 수집·정규화·GCS parquet 적재

## 아카이브

완료된 spec/plan과 과거 리포트(중간발표, QA·실증 테스트 리포트)는
[`archive/`](archive/)에 있습니다. 역사적 기록이므로 갱신하지 않습니다.
