# 문서 인덱스

이 저장소의 문서는 아래 규칙으로 배치합니다.

| 위치 | 내용 | 수명 |
|---|---|---|
| `adr/` | 아키텍처 결정 기록 (ADR) | 영구 |
| `specs/` | 살아있는 계약·설계 spec (`YYYY-MM-DD-<slug>.md`) | 유효한 동안 |
| `plans/` | 진행 중 구현 계획 (`YYYY-MM-DD-<slug>.md`) | 구현 완료 시 archive로 |
| `guides/` | 운영·아키텍처 가이드 | 상시 갱신 |
| `assets/` | 문서용 이미지 | 참조되는 동안 |
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
- [Plan — `src/` → `autoresearch/` 패키지 통합](plans/2026-07-15-src-package-merge.md) (팀 합의 대기)
- `src/pipeline/`, `src/models/`, `src/features/` (CTR 학습·평가 코드)

### 🚀 서빙 (Serving)

- [Spec — YouTube 리랭킹 서빙 API](specs/2026-07-16-reranking-serving-api.md)
- [Plan — Reranking Serving API 구현](plans/2026-07-16-reranking-serving-api.md) (완료)
- `src/serving/` (FastAPI 추론 서버), `deploy/serving/` (이미지 정의)

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

## ADR

- [0001 — YouTube 프록시의 목적](adr/0001-youtube-proxy-purpose.md)
- [0002 — 저장소 책임 경계](adr/0002-repository-responsibility-boundaries.md)

## 유효한 Spec (살아있는 계약)

- [공개 batch 실행 계약](specs/2026-07-13-public-batch-execution-contract.md) —
  Airflow가 소비하는 공개 CLI·인자 계약
- [Autoresearch-airflow 경계 컷오버](specs/2026-07-13-autoresearch-airflow-boundary-cutover.md)
- [MLflow 배포 전략](specs/2026-07-14-mlflow-deployment-strategy.md)
- [GCS raw 데이터 BigQuery 적재](specs/2026-07-11-load-raw-to-bigquery.md)
- [저장소 구조 재정리](specs/2026-07-15-repo-restructure.md) — 이 문서 구조의 근거,
  `src/` 패키지 통합 목표 구조 포함

## 가이드

- [데이터 레이크](guides/data-lake.md) · [데이터 웨어하우스](guides/data-warehouse.md)
- [학습 데이터셋](guides/training-dataset.md)
- [피처 스토어](guides/feature-store.md) · [Feast GCP 설정](guides/feast-gcp-setup.md)
- [CTR 모델 명세](guides/ctr-model-specification.md)
- [Agent Simulator 명세 (action log SSOT)](guides/agent-simulator-spec.md)
- [action_logs 모듈 사용법](guides/action-log.md)
- [Release & 배포 파이프라인](guides/release-pipeline.md) — CI/CD·GAR push·digest 승격·GKE 배포 자동화
- [CTR 학습 이미지](guides/training-image.md) — `Dockerfile.train`, MLflow tracking URI 연동
- [YouTube 트렌딩 수집 파이프라인](guides/youtube-collection.md) — API 수집·정규화·GCS parquet 적재

## 아카이브

완료된 spec/plan과 과거 리포트(중간발표, QA·실증 테스트 리포트)는
[`archive/`](archive/)에 있습니다. 역사적 기록이므로 갱신하지 않습니다.
