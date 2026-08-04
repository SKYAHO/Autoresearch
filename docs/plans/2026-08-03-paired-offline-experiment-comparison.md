# paired offline 실험 배치·비교 결과 구현 계획 (#454)

정본 계약: `docs/specs/2026-08-03-paired-offline-experiment-comparison.md`

## 파일별 책임

| 파일 | 변경 |
| --- | --- |
| `autoresearch/experiments/context.py` | 조건(`baseline`/`candidate`) 격리 좌표, legacy 좌표 파서 |
| `src/features/feast_retrieval.py` | 추가 피처 보존 검증 헬퍼 |
| `src/pipeline/build_training_dataset.py` | `feature_service`·`extra_features` 관통, CSV 컬럼 확장 |
| `src/cli.py` | `--feature-service`/`--extra-features` 조립 전달, `compare-paired-experiment` 명령 |
| `src/pipeline/paired_experiment.py` | 요청·결과 모델, 검증, 판정 사상, payload 생성 |
| `.github/workflows/auto-research-promotion.yml` | 조건 격리 registry 경로 수용 |
| 문서 | spec/plan, `docs/README.md`, `README.md`, `.claude/docs/agent-project-reference.md`, 공개 batch 계약 |

## Task 1 — 조건 격리 실행 context

- [ ] `ExperimentContext`에 `condition`·`source_sha`를 도입하고 `registry_key`를
      `experiments/<issue>/<experiment_id>/<condition>/<source_sha>/registry.db`로 만든다.
- [ ] `parse_registry_key`(legacy/조건 격리 양쪽 인식)를 추가한다.
- [ ] 잘못된 condition·SHA·run_id를 거부하는 테스트를 추가한다.

검증: `uv run python -m pytest tests/test_experiment_context.py -v`

## Task 2 — 데이터 조립 피처 보존

- [ ] `build_training_dataset.main`/`_assemble_via_feast`에 `feature_service`,
      `extra_features` 파라미터를 추가하고 `retrieve_training_features(service=...)`로 전달한다.
- [ ] CSV 저장 컬럼을 `[*MODEL_FEATURE_COLUMNS, *extra_features, "clicked"]`로 확장하고,
      누락 시 CSV를 쓰기 전에 fail-closed한다.
- [ ] snapshot manifest에 실제 `feature_service`를 기록한다.
- [ ] 중복·prod 계약 충돌 extra feature 이름을 거부한다.
- [ ] 기존 22컬럼 계약 테스트를 유지하고, 확장 경로 테스트를 추가한다.

검증: `uv run python -m pytest tests/test_build_training_dataset_feast_path.py -v`

## Task 3 — paired 비교 판정 모듈

- [ ] 요청 모델(`PairedExperimentRequest`)과 결과 모델(`PairedExperimentResult`)을 정의한다.
- [ ] 요청 검증(SHA·형식·조건 좌표·code archive·seed 집합)을 구현한다.
- [ ] seed별 `verify_training_comparison` 재검증과 누락 쌍 판정을 구현한다.
- [ ] `create_paired_seed_evidence` → `evaluate_experiment` → `decide_promotion`을 호출하고
      `eligible|reject|hold`를 `comparison_passed|comparison_rejected|comparison_failed`로 사상한다.
- [ ] 검증 실패는 판정 엔진 호출 없이 `comparison_failed`로 끝낸다.
- [ ] 결과 payload를 원자적으로 기록한다.

검증: `uv run python -m pytest tests/test_paired_experiment.py -v`

## Task 4 — 공개 CLI 명령

- [ ] `compare-paired-experiment` 명령을 추가한다 (`--request`, `--promotion-evidence-root`, `--output`).
- [ ] 실패해도 결과 파일과 사유를 남기고 exit code로 구분한다.
- [ ] 예외 원문(자격증명·signed URL)을 CLI 출력에 복사하지 않는다.

검증: `uv run python -m pytest tests/test_cli.py -v`

## Task 5 — 승격 게이트 경로 수용

- [ ] `auto-research-promotion.yml`이 조건 격리 candidate 경로와 legacy 경로를 모두 통과시킨다.
- [ ] baseline 조건 경로는 거부한다.
- [ ] 워크플로우 계약 테스트를 갱신한다.

검증: `uv run python -m pytest tests/test_experiment_promotion_gate.py -v`

## Task 6 — 문서

- [ ] spec/plan을 추가하고 `docs/README.md` 인덱스를 갱신한다.
- [ ] 새 공개 명령을 `docs/specs/2026-07-13-public-batch-execution-contract.md`,
      `README.md`, `.claude/docs/agent-project-reference.md`에 반영한다.
- [ ] `docs/guides/training-experiment-provenance.md`에 paired 실행 경로를 연결한다.

검증: `git diff --check`

## 전체 검증

```bash
uv run python -m pytest -v
uv run --no-sync ruff check agent_orchestration autoresearch tests tools
```

로컬에 `libomp`가 없으면 LightGBM 의존 테스트 4개 파일이 수집 단계에서 실패한다
(환경 문제, 변경과 무관). 해당 파일은 CI에서 검증한다.
