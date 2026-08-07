# 실험 실행 능력 활성화 구현 계획

**목표:** 배포된 executor가 실제로 학습·측정하게 만들고, 에이전트가 실험 하네스를
설계·구현하는 과정을 관측 가능하게 한다.

**아키텍처:** 기존 8-container executor Job과 통계 판정 엔진을 유지한다. 새 실행
경로를 만들지 않고, `phase2.py`에 이미 있는 학습 호출부를 배선·권한으로 활성화한 뒤
관측성·산출물 계약·판정을 순차 확장한다.

**기술 스택:** Python 3.12, Kubernetes Job, GCS(content-addressed 스냅샷), BigQuery,
SQLAlchemy/Alembic, PostgreSQL, Codex CLI, uv, pytest, Ruff.

## 전역 제약

- 구현 정본은 `docs/specs/2026-08-07-experiment-execution-enablement.md`다.
- **한 단계씩 완주하고 다음으로 넘어간다.** 여러 층을 동시에 켜면 실패 원인이 겹쳐
  규명이 불가능해진다 — 현재 상태가 정확히 그 결과다.
- 실행 Job 배선과 IAM은 `Autoresearch-infra`·`Autoresearch-airflow` 소유다. 이 저장소
  작업과 구분해 추적한다.
- 클러스터 변경은 dev에 한정한다. prod 좌표를 건드리지 않는다.

## Stage 0 — 선행 정리 (차단 요소 제거)

- [ ] **시크릿 회전.** 2026-08-07 진단 중 API Pod env 덤프로 노출됨:
      `ORCH_GITHUB_TOKEN`(github_pat), `ORCH_API_TOKEN`, `ORCH_RUNNER_TOKEN`,
      `ORCH_EXECUTOR_API_TOKEN` 4개
- [ ] 좀비 실험 정리. `RUNNING` 7건 중 `ar-branch-*`(Phase 1 네이밍) 2건은 하루 이상
      경과. launcher의 `reconcile_failed_jobs`가 회수하도록 할지 수동 정리할지 결정
- [ ] `agent-orchestration-launcher` CronJob `suspend=false` 복귀 판단.
      7시간 전 실패 원인이던 `candidate_sha` 스키마는 해소됨
      (`alembic_version=0005_experiment_candidate_sha` 확인). 이미지도 새 digest로 교체됨
- [ ] 재개 전 `ORCH_MAX_CONCURRENT_EXPERIMENTS=2`와 `CREATED` 13건의 관계 확인 —
      재개 즉시 대기열이 소진되며 실패가 양산될 수 있다

## Stage 1 — B: 데이터·학습 활성화

### 1-1. 스냅샷 게시

- [ ] dev 좌표로 조립 1회 실행 → `publish_snapshot()`으로 `by-hash/<sha256>/` 게시
- [ ] `record_pointer=False` 확인 — 실험 조립이 `by-date` 포인터를 갱신하면 prod
      재학습 경로와 경합한다
- [ ] 게시된 스냅샷의 표본 규모 기록(행수·양성수·기간). 로컬 실증의 양성 36건은
      결론을 지탱하지 못했다

### 1-2. 권한

- [ ] `experiment-job` GSA에 스냅샷 root **read** 부여 (현재 `objectCreator`만) — infra
- [ ] Pod에서 `download_snapshot()` 성공 확인

### 1-3. 배선

- [ ] launcher가 executor Job에 dataset URI 주입 → `ORCH_TRAINING_DATASET_PATH`
- [ ] `ORCH_TRAINING_TIMEOUT_SEC`, `ORCH_UV_SYNC_TIMEOUT_SEC` 설정
- [ ] baseline marker 순서 강제 동작 확인 (`training.py:220`)

### 1-4. 시간 예산 재계산 — 완료 (2026-08-07)

- [x] 실측: 학습 1회 소요 × `POLICY_SEEDS` × 2조건
- [x] `activeDeadlineSeconds`(현 3600) 초과 여부 판단
- [x] MVP `POLICY_SEEDS` 결정

**실측 조건**: 3일치 스냅샷(494,472행 / 양성 7,274), `taskset -c 0`으로 1코어 제한
(executor Pod `limits.cpu=1` 근사), executor가 실제로 쓰는 `src.cli train-model`
명령과 동일 형태(`training.py:224`).

| 지표 | 실측값 | 대비 |
|---|---:|---|
| 학습 1회 (1코어) | **19.5초** | 20코어에서는 16초 |
| 피크 메모리 | **760 MB** | `limits.memory=2Gi`, `requests=1.5Gi` 안 |
| CPU 사용률 | 97% | 1코어를 실제로 다 씀 |
| Val ROC-AUC | 0.6295 | 재현됨 |

**메모리**: `jobs.py:_container_resources()` 주석의 기존 실측은 학습 피크 1.22 GiB였으나,
이는 **Pod 안에서 조립까지 하던 시절**의 값이다. 조립을 Pod 밖으로 뺀 현행 설계에서는
760 MB로 내려간다. request 1.5Gi 안에 들어오므로 QoS Burstable eviction 위험도 없다.

**시간**: 3일치 기준 seed 30개를 복원해도 1,170초로 상한의 33%다. Codex 상한
1800초(`ORCH_CODEX_TIMEOUT_SEC`)를 더해도 2,970초로 3600초 안이다. 기존 주석의
"30 seed = 4,068초로 상한 초과" 근거는 **12일치 데이터 기준**(학습 1회 67.8초)이었고,
3일치에서는 성립하지 않는다.

**결정 — MVP는 `POLICY_SEEDS=3`을 유지한다.** 시간·메모리 여유가 확인됐지만 MVP의
목적은 판정 정확도가 아니라 "한 바퀴가 도는 것"의 증명이다. 판정 검정력 개선은
아래 후속으로 미룬다.

- [ ] **(후속) `POLICY_SEEDS` 상향.** 현행 3개는 자유도 2, t_critical≈4.30으로
      신뢰구간이 극단적으로 넓어 실제 개선도 `HOLD`(=실패)로 떨어진다. 10개면
      자유도 9·t_critical≈2.26으로 판정이 의미를 갖기 시작하고 학습 시간은 390초다.
      30개(원래 값) 복원도 3일치 기준으로는 가능하다. 학습 윈도우를 늘리면 재계산이
      필요하다 — 소요는 데이터 크기에 거의 선형이다(3일 19.5초 ↔ 12일 67.8초)

### 검증

- [ ] 실험 1건이 `candidate_sha`가 채워진 채로 완주 — **현재까지 0건**
- [ ] baseline/candidate 산출물이 seed별로 `data/processed/{baseline,candidate}/`에 생성
- [ ] `uv run python -m pytest`, `uv run --no-sync ruff check ...`

## Stage 2 — D: 관측성 + 컨테이너 통합

Stage 1이 성공해도 실패해도, **왜 그런지 보이게** 하는 것이 목적이다. 컨테이너 통합을
함께 수행한다 — 5·6번의 보고 경로 문제에 대한 가장 단순한 해답이 통합이기 때문이다.

### 2-1. 컨테이너 통합

계약 근거는 spec의 §컨테이너 구성 범위다.

- [ ] 토큰 minter 3개는 **분리 유지**. private key가 이 셋에만 들어간다
- [ ] `workspace-preparer` + `codex-worker` + `candidate-verifier` + 학습 +
      `candidate-finalizer`를 하나로 통합. 토큰 볼륨은 필요한 시점에만 마운트
- [ ] 통합 후에도 다음이 유지되는지 확인: `read_only_root_filesystem=True`,
      `allow_privilege_escalation=False`, `capabilities.drop=["ALL"]`,
      `automount_service_account_token=False`
- [ ] Codex 실행 시점에 push 토큰 볼륨이 마운트되어 있지 않은지 명시적으로 검증
- [ ] 학습을 `workspace-preparer`/`candidate-finalizer` 안이 아니라 자기 스테이지로
      분리 (`phase2.py:181`, `:301`)
- [ ] 재시도 단위 재정의 — Job `backoffLimit`이 Codex를 재호출하지 않도록

### 2-2. 관측성

- [ ] codex-worker 최소 진단 출력: exit code, 소요 시간, 변경 파일 수, stdout 말미 요약.
      원문 전체는 남기지 않는다(`codex_worker.py:294`의 경계 유지)
- [ ] Step API 기록 배선 — `FEATURE_ASSEMBLY`/`TRAIN`/`EVALUATE`/`OTHER`.
      계약(#518)은 이미 있고 사용처가 없다. 통합 후에는 같은 프로세스가 직접 보고한다
- [ ] 실험 컨텍스트(`experiment_id`/`issue_number`/`branch`)가 전 스테이지 로그에
      실리는지 확인 — 현재 codex-worker·verifier가 `unknown`으로 찍힌다
- [ ] `ORCH_TTL_AFTER_FINISHED_SEC` 재검토. 30초는 사후 진단을 사실상 불가능하게 한다

### 검증

- [ ] `experiment_steps`에 실험당 최소 1행 이상 기록 — **현재 전 실험 공백**
- [ ] `no_changes` 재발 시 원인이 로그로 판별 가능한지 확인
- [ ] 통합 전후로 실험 1건 완주 결과가 동일한지 회귀 확인
- [ ] `uv run python -m pytest`, `uv run --no-sync ruff check ...`

## Stage 3 — A: 산출물 계약 변경

- [ ] 하네스 디렉터리 위치·이름 규칙 확정. 로컬 실증의 `capability_probe/round_NNN_.../`
      패턴을 참고
- [ ] 결과 JSON 스키마 정의 (탐색 격자, 우승 조합, 조건별 지표, seed 목록)
- [ ] `verifier.py` 정책 확장 — 하네스 경로 허용, `no_changes` 판정 기준 재정의
- [ ] Codex 프롬프트(`prompt.py`)의 허용/금지 경로와 지시문 갱신
- [ ] 이슈 폼 `allowed_scope`와 실제 실행 가능 범위 정합. 현재 *"Feast 정의 수정 허용"*
      체크박스가 있으나 1단계에서는 학습이 거부한다

## Stage 4 — C: 판정 확장

- [ ] 기여 분해(피처만/모델만/상호작용) 산출·기록. 로컬에서 이 분해가 "피처 가설 성공"
      오판을 잡았다
- [ ] 보조 지표 가드 — LogLoss·Brier 악화 임계 정의 및 fail-closed 연결.
      로컬 우승안은 LogLoss 0.0875→0.1671, Brier 0.0132→0.0479로 캘리브레이션이 붕괴했다
- [ ] 에이전트 주장 지표를 신뢰하지 않고 독립 재현하는 경로 확정
- [ ] `POLICY_SEEDS` 복원 여부 결정 (Stage 1-4 실측 반영)

## Stage 5 — report.md

프로젝트의 목표 산출물이며 현재 생성 경로가 없다. 결과가 나가는 곳은 GitHub 이슈 본문
`결과` 항목과 Experiment API뿐이다.

- [ ] `PairedExperimentResult`를 입력으로 하는 Markdown 렌더러
- [ ] 포함 항목: 가설, 탐색 공간, 우승 조합, before/after 지표, 기여 분해, 보조 지표,
      seed·분할 정보, 판정과 사유, 데이터 provenance
- [ ] 사람이 승격을 판단할 수 있는 정보가 빠짐없이 담기는지 검토

## Stage 6 — 실험별 registry (후속, 별도 계획으로 분리 가능)

계약은 이미 존재한다(#454). 실행 Job 배선만 남았고 소유는 다른 저장소다.

- [ ] `feast apply` + `build-features`를 실험별 registry에 수행하는 Job — airflow
- [ ] registry root 쓰기 권한 — infra
- [ ] `feature_change_unsupported` 예외 해제 조건 정의
- [ ] 조립 비용·시간 실측 후 공유 스냅샷과의 사용 기준 정리

## 후속 (MVP 범위 밖)

우선순위가 낮아 미룬 항목이다. MVP 동작에 필요하지 않다.

- [ ] **`CREATED → ERROR` 전이 허용.** 현재 `ALLOWED_TRANSITIONS`에서 `CREATED`는
      `RUNNING`으로만 나갈 수 있어(`models.py:84`), 이슈 발행에 실패해 선점 조건을
      갖추지 못한 레코드를 API로 닫을 수단이 없다. 2026-08-07 정리에서는 SQL 직접
      삭제로 처리했다(#600).
      - 선택지 1: `CREATED`의 허용 집합에 `ERROR` 추가. 간단하나 "실행 중 오류"와
        "발행 실패 폐기"가 같은 상태로 섞인다. `reason`으로 구분 가능
      - 선택지 2: `CANCELLED` 등 폐기 전용 상태 신설. 의미는 명확하나 마이그레이션·
        스키마·UI 영향이 따른다
      - 현행 설계가 의도적일 가능성을 먼저 확인한다 — "아직 시작하지 않은 것은 실패할
        것이 없다"는 관점일 수 있다

## 진행 원칙

각 Stage 완료 시 **다음 Stage로 넘어가기 전에** 실험 1건을 끝까지 돌려 회귀를 확인한다.
Stage 1이 끝나면 "학습이 도는가", Stage 2가 끝나면 "왜 그런지 보이는가", Stage 3이
끝나면 "에이전트 산출물이 하네스인가"가 각각 독립적으로 증명되어야 한다.
