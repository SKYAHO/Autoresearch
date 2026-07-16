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

## 아카이브

완료된 spec/plan과 과거 리포트(중간발표, QA·실증 테스트 리포트)는
[`archive/`](archive/)에 있습니다. 역사적 기록이므로 갱신하지 않습니다.
