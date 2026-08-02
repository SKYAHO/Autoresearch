# GCP 프로젝트 선택 fail-fast 계약

- **상태**: Accepted
- **날짜**: 2026-08-01
- **관련 이슈**: #416

## 목적

BigQuery 프로젝트를 코드 리터럴로 선택하던 경로를 제거한다. 프로젝트 값이
누락되면 BigQuery, Feast 또는 GCP 자격증명 확인보다 먼저 실행자가 설정할 값과
대안을 알 수 있는 오류로 중단해야 한다.

## 계약

| 경로 | 허용하는 프로젝트 입력 | 미설정 동작 |
| --- | --- | --- |
| `python -m autoresearch.jobs.feature_store_build` | `--project`, `CTR_TRAINING_BQ_PROJECT` | 종료 코드 2, 두 입력명을 포함한 인자 오류 |
| `scripts/build_static_features.py` | `--project`, `GCP_PROJECT_ID` | argparse 종료 코드 2, 두 입력명을 포함한 오류 |
| `src.pipeline.build_training_dataset.py` | `CTR_TRAINING_BQ_PROJECT` | `ValueError`, 환경 변수명을 포함한 오류 |

세 경로 모두 프로젝트 ID 리터럴 fallback을 두지 않는다. 명시 인자가 있는 CLI는
인자를 우선하고, 환경 변수는 그 다음 수단이다. 학습 데이터셋 조립은 공개
`--project` 인자를 제공하지 않으므로 `CTR_TRAINING_BQ_PROJECT`만 허용한다.

## 구현 경계

- `feature_store_build`의 `BatchArgumentError` 및 JSON summary 계약은 유지한다.
- `build_static_features`의 기존 argparse 오류 방식을 유지한다.
- 학습 데이터셋 모듈은 BigQuery를 쓰는 모든 경로에서 같은 프로젝트 검증을
  재사용한다. `main()`은 기존 환경 검증의 첫 단계에서 이를 실행한다.
- `load_events_from_bigquery`, `load_training_entity_spine`, `_assemble_via_feast`처럼
  직접 호출 가능한 경로도 BigQuery/Feast 생성 전에 같은 검증을 실행한다.
- `scripts/backfill_feature_store.py`는 이미 `GCP_PROJECT_ID` 또는 `--project`를
  검증하므로 코드 동작은 바꾸지 않고 사용 예시만 플레이스홀더로 바꾼다.

## 문서 계약

`.env.example`은 `CTR_TRAINING_BQ_PROJECT`를 빈 필수값으로 보여 준다. 현재 운영
문서와 살아있는 feature-store batch spec은 프로젝트가 필수 설정임을 서술하고,
Artifact Registry 명령은 고정 프로젝트 ID 대신 환경 변수 플레이스홀더를 쓴다.
과거 실행을 설명하는 runbook 기록은 이 변경의 대상이 아니다.

## 검증

- 각 미설정 경로는 명확한 오류와 함께 BigQuery 클라이언트 생성 전에 실패한다.
- 각 명시 `--project` 입력은 기존 성공 경로를 유지한다.
- 런타임 코드에 `ar-infra-*` 또는 `autoresearch-*` 형식의 프로젝트 ID 리터럴이
  남지 않는다.
- 과거 계획, spec, archive 및 `docs/runbooks/2026-07-23-action-log-feature-loop.md`의
  과거 실행 기록을 제외한 현재 운영 문서에 옛 프로젝트 ID가 남지 않는다.
