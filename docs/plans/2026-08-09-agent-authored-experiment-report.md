# 에이전트가 리포트를 쓰는 실험 실행 — 구현 순서

> 2026-08-09 | 계약: `docs/specs/2026-08-09-agent-authored-experiment-report.md`
>
> `docs/plans/2026-08-07-experiment-execution-enablement.md`의 Stage 2~6을 대체한다.
> Stage 0(선행 정리)·Stage 1(데이터·학습 활성화)은 완료·유효하다.

## 전역 제약

- **각 Stage 완료 시 다음으로 넘어가기 전에 실험 1건을 끝까지 돌려 회귀를 확인한다.**
- **한 Stage씩만 켠다.** 여러 층을 동시에 켜면 실패 원인이 겹쳐 규명이 불가능하다.
  2026-08-08에 네 겹이 겹쳐 있던 것을 하나씩 걷어내며 배운 원칙이다.
- Stage 1이 끝나면 "숫자가 남는가", Stage 2가 끝나면 "통합해도 같은 숫자인가",
  Stage 3이 끝나면 "에이전트가 리포트를 쓰는가"가 각각 독립적으로 증명되어야 한다.

## Stage 0 — 선행 (다른 저장소·수동)

- [ ] **`Autoresearch-infra` 매니페스트에 `ORCH_ACTIVE_DEADLINE_SEC=60000`,
      `ORCH_CODEX_TIMEOUT_SEC=6000` 반영.** 이 저장소의 `.env.example`은 갱신됨.
      실제 적용은 infra 쪽 launcher CronJob env를 바꿔야 한다
- [ ] **NetworkPolicy Git 정본 반영.** 2026-08-08에 클러스터에서 직접 고친
      `agent-orchestration-api-egress`의 executor ingress 규칙이 infra 저장소
      `deploy/agent-orchestration/network-policy.yaml`에 없으면 **Argo sync 때
      되돌아가고 다음 실험이 `candidate_api_failed`로 떨어진다**
- [ ] **시크릿 회전** (미해결 이월). `ORCH_GITHUB_TOKEN`, `ORCH_API_TOKEN`,
      `ORCH_RUNNER_TOKEN`, `ORCH_EXECUTOR_API_TOKEN` 4개. 사용자가 직접 수행

## Stage 1 — 측정 + 게시 (컨테이너 통합 없이)

**목표: 실험이 끝난 뒤 숫자가 남는다.** 현재 `metric_summary=null`, `steps=0건`이다.

### 1-1. 기존 평가 경로 재사용

**새 측정 모듈을 만들지 않는다.** `src/pipeline/evaluate.py`가 이미 ROC-AUC ·
LogLoss · Brier · PR-AUC · grouped ROC-AUC를 계산하고 `evaluate-model` CLI도 있다.
executor가 `train-model`을 부르는 것과 **같은 패턴**으로 부른다.

무결성은 하네스 지침(AGENTS.md, Stage 3)이 담당한다. 채점 경로를 손대면 **diff에
드러나 Claude 리뷰가 잡는다.** 코드 레벨 봉인을 겹치지 않는다.

> 두 조건을 **같은 채점자**가 채점하므로 candidate 코드로 평가해도 delta는 유효하다.

- [x] `src/cli.py`의 `evaluate-model`에 **`--metrics-output <FILE>`** 추가 —
      지표를 JSON으로 기록한다. 현재 `evaluate.main`은 stdout에 print만 해서
      파싱이 깨지기 쉽다. **숫자를 코드가 계산해 파일에 쓰게 하려는 것이지
      보호 목적이 아니다**
- [x] `src/pipeline/evaluate.py`가 그 파일을 쓰도록 배선 — `write_held_out_metrics()`,
      계약 `held-out-metrics-v1`. 임시 파일 + `os.replace`로 원자 게시(부분 파일 없음)

### 1-2. executor의 측정 단계

- [ ] `agent_orchestration/executor/training.py`의 `_run` 패턴을 따라
      `evaluate-model`을 **6회**(2조건 × 3seed) 호출하고 결과 JSON을 수집
- [ ] `metrics.json` 조립
      - seed별 baseline·candidate의 ROC-AUC · LogLoss · Brier
      - 참고 통계: `compare_to_baseline` · `summarize_metric` · `t_critical_95`.
        **판정하지 않는다 — 수치만 낸다**
      - `split_hash`(두 조건의 테스트셋 동일성) · `dataset_fingerprint`
      - 실험 좌표: `experiment_id` · `issue_number` · `base_dev_sha` ·
        `candidate_sha` · `image_digest` · seed 목록

### 1-3. GCS 게시

- [ ] 버킷 `gs://autoresearch-503903-autoresearch-dev-experiment-results`.
      `exp-job` GSA가 `objectCreator`(**교체 불가**)+`objectViewer`만 가짐 — 확인됨
- [ ] 경로 규칙 확정: `experiments/{issue_number}/{experiment_id}/...`
- [ ] `metrics.json` · 모델 · 테스트셋 · 검증 결과 게시
- [ ] launcher가 버킷 URI를 env로 주입 (신규 env → `README.md`·
      `.claude/docs/agent-project-reference.md` 동반 갱신 대상)

### 1-4. 배선

- [ ] `phase2.py`에 측정 단계 추가. **현행 8-container 구조 그대로** 붙인다
      (candidate 학습이 있는 `candidate-finalizer` 뒤)
- [ ] `evaluate-model`이 이미지의 어느 python·라이브러리로 도는지 확인한다.
      `train-model`과 같아야 학습·평가가 어긋나지 않는다 (`uv sync`는 workspace를
      대상으로 하고 `_run`은 PATH의 python을 쓴다 — 지금 같은 환경인지 미확인)
- [ ] Experiment API `metric_snapshot`에 지표 반영

### 검증

- [ ] 실험 1건이 `metrics.json`을 GCS에 남긴 채 완주 — **현재 0건**
- [ ] `metric_summary`가 `null`이 아니다 — **현재 전 실험 `null`**
- [ ] **같은 `base_dev_sha`로 2건을 돌려 baseline seed별 값이 일치하는지 관측한다.**
      일치하면 baseline 캐싱이 정당화되고, 어긋나면 `+0.003` 수준 개선의 신뢰성부터
      다시 봐야 한다 — 캐싱보다 중요한 발견이다
- [ ] `uv run python -m pytest`, `uv run --no-sync ruff check agent_orchestration autoresearch tests tools`

## Stage 2 — 컨테이너 8 → 4

**목표: 통합해도 같은 숫자가 나온다.** Stage 1이 있어야 이 회귀를 확인할 수 있다.

- [ ] `codex_worker._capture_protected_git_metadata`에서 **마운트 검사만** 제거.
      `_git_metadata_digest` 전후 대조는 유지 (예방 → 탐지)
- [ ] `jobs.py` 재작성 — 4 컨테이너

  | # | 컨테이너 | 내용 | credential |
  |---|---|---|---|
  | ① | token-minter | branch·clone 토큰 동시 발급 | App private key |
  | ② | worker | clone → baseline 학습 → Codex → 검증 → candidate 학습 → 측정 | 없음 |
  | ③ | push-token-minter | push 토큰 | App private key |
  | ④ | finalizer | commit → push → 게시 → API 보고 | push·API 토큰 |

- [ ] `branch-creator`를 ②로 흡수 (재시도 시 2회 실행되던 문제도 함께 정리)
- [ ] `candidate-verifier`를 ②로 흡수 — **pytest 중복(Codex 8분 + verifier 8분)
      해소**
- [ ] **candidate 학습을 push 앞으로 이동.** 지금은 push 후라 #618처럼 push 성공 +
      보고 실패로 커밋이 고아가 된다. 이동하면 "검증 안 된 결과로 push하지 않는다"가
      성립한다
- [ ] 컨테이너 간 핸드오프였던 `executor-state` marker · `verification-result`
      볼륨을 프로세스 내 상태로 단순화
- [ ] 검증 실패를 Codex에 되돌려 재시도 (**최대 2회**). 되돌리는 대상은
      `git diff --check`·Ruff·구문 오류까지. **경로 위반·credential 검출·git digest
      불일치는 즉시 종료** — 실수가 아니라 경계 침범이다

### 검증

- [ ] 통합 **전후** 실험의 `metrics.json` seed별 값이 일치
- [ ] `experiment_id=unknown` 소멸, `experiment_steps`에 실험당 1행 이상 —
      **현재 전 실험 공백**
- [ ] pytest·ruff

## Stage 3 — AGENTS.md + Codex 2회 + `report.md`

**목표: 에이전트가 자기 실험 결과로 리포트를 쓴다.**

### 3-1. AGENTS.md 교체 (현행 버그 수정)

- [ ] executor 전용 하네스 지침 작성 — 실험 하네스가 무엇인지, 산출물이 무엇인지,
      `report.md` 형식, **작업 범위(경로)**
- [ ] ②가 clone 직후 **Codex 실행 전에** 교체. 그 시점 워크스페이스가 검증
      baseline이므로 verifier가 Codex 변경으로 오인하지 않는다.
      **`AGENTS.md`는 루트라 기존 경로 정책상 Codex가 못 고친다** — 별도 보호 불필요
- [ ] `prompt.py`의 허용·금지 경로와 지시문을 이 파일로 이관
- [ ] **verifier의 정책 강제를 걷어낸다** — 경로 allowlist(`_path_is_allowed`),
      credential 내용 검사(`_content_is_forbidden`), symlink·submodule 거부.
      **관측·핸드오프는 유지**: 변경 파일 목록 · `no_changes` 판정 · staged tree OID ·
      content fingerprint · pytest 관측치
- [ ] **크기 상한은 유지** — 변경 50개 / diff 1 MiB / 파일 10 MiB /
      `.csv`·`.pkl`·`.parquet` 거부. 저장소 위생 목적
- [ ] **`prompt.py`와 verifier의 불일치 해소** — 프롬프트는 `pyproject.toml`·
      `uv.lock` 금지를 알려주지 않는데 verifier는 막는다. Codex가 의존성을 바꾸면
      이유도 모른 채 거부된다. 하네스에 명시하거나 허용으로 바꾼다
      (연동: `training.py`의 `dependencies_changed`·`sync_dependencies`는 현재
      이 거부 때문에 도달할 수 없는 경로다)

### 3-2. Codex 2회 호출

- [ ] `--ephemeral` 제거
- [ ] **두 호출이 같은 `CODEX_HOME`을 공유하도록 변경.** 현행은 호출마다
      `TemporaryDirectory`를 만들어 세션이 이어지지 않는다
- [ ] `codex exec resume --last`로 2회차 (0.146.0에 존재 — 확인됨)
- [ ] `--output-schema <FILE>`로 최종 응답 형태 강제, `-o`로 파일 수신.
      현행 stdout tail 64 KiB 스크래핑을 대체
- [ ] `report.md` 형식 검사 → 실패 시 되돌려 재시도(최대 2회)

### 3-3. `report.md` 내용 계약

- [ ] 가설 / 무엇을 어떻게 바꿨는지 / before-after 주 지표 / 보조 지표 /
      seed별 표 / 데이터·분할 provenance / 에이전트의 결론과 근거

### 검증

- [ ] `report.md`가 GCS에 남고, 그 안의 숫자가 `metrics.json`과 일치
- [ ] AGENTS.md 충돌로 인한 `no_changes`가 재발하지 않음
- [ ] pytest·ruff

## Stage 4 — Claude 리뷰어 (Pod 밖)

**목표: 통계가 못 잡는 것을 잡는다.** 리크된 실험은 paired t-test를 가장 깨끗하게
통과한다.

- [ ] 실행 위치 결정 — 기존 Codex Runner 경로 재사용 여부
- [ ] 입력: 가설 원문 · `metrics.json` · candidate diff · `report.md`
- [ ] 지침: **코드 주석과 리포트 서술은 사실이 아니라 주장으로 취급**한다.
      가능한 항목은 추론이 아니라 검사로 답한다(테스트셋 오염 → 멤버십 해시 대조)
- [ ] 답할 질문 5개: 가설 정합 / 리크 / 측정 무결성 / 개선의 출처 / 부작용
- [ ] 출력은 `report.md`에 병기. **MVP에서는 상태를 바꾸지 않는다**

### 검증

- [ ] 리뷰 결과가 `report.md`에 실려 GCS에 남는다
- [ ] 리뷰어가 리뷰 대상에 쓰기 권한이 없음을 확인

## 상태 전이 (Stage 1에서 함께)

- [ ] `PASSED` = 실험이 완주하고 결과가 나왔다 / `FAILED` = 실행 실패로 결과 없음
- [ ] 가설의 성패는 `report.md`가 서술. 지표는 `metric_snapshot`으로 워크벤치 노출
- [ ] 이 경로 전용 결과 계약 정의 — `PairedExperimentResult`의
      `ConditionLineage` 등 격리 실행 모델 필드를 그럴듯하게 채우지 않는다

## MVP 범위 밖

- Claude 리뷰어의 **거부권** — 판정이 숫자와 일관되게 움직이는지 실적이 쌓인 뒤
- **baseline 학습 캐싱** — Stage 1 검증에서 재현성이 확인된 뒤
- `tests/**` 쓰기 권한 회수 / 하네스 격리 디렉터리로의 산출물 전환
- 피처 변경 실험 해금 (`feature_change_unsupported`)
- `CREATED → ERROR` 전이
- `POLICY_SEEDS` 상향 — `ORCH_ACTIVE_DEADLINE_SEC` 60000이면 여유가 생겼으나
  Stage 1 실측 후 결정한다
