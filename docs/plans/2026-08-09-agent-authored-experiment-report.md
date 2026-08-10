# 에이전트가 리포트를 쓰는 실험 실행 — 구현 순서

> 2026-08-09 | 계약: `docs/specs/2026-08-09-agent-authored-experiment-report.md`
>
> `docs/plans/2026-08-07-experiment-execution-enablement.md`의 Stage 2~6을 대체한다.
> Stage 0(선행 정리)·Stage 1(데이터·학습 활성화)은 완료·유효하다.

## 실행 순서 (2026-08-09 변경)

**Stage 1 → Stage 3 → Stage 2 → Stage 4로 진행한다.** 아래 문서의 절 번호는 그대로
두되, 착수 순서만 바꾼다(#639).

Stage 2(컨테이너 8 → 4)는 통합·중복 제거·재시도 정리이고 **MVP의 기능을 늘리지
않는다.** Stage 2가 앞에 있던 유일한 근거는 "Codex 두 호출이 같은 컨테이너에 있어야
한다"였는데, `report.md`는 git 커밋 대상이 아니라 GCS 게시 산출물이므로(계약 결정 5)
Codex #2가 push 뒤 `candidate-finalizer` 안에 들어가면 된다. 컨테이너 재구성이 필요
없다.

```
candidate-finalizer 안에서
  push → candidate 학습 → 채점 → metrics.json
       → Codex #2: metrics.json + candidate diff로 report.md 작성
       → GCS 게시 (metrics.json + report.md)
```

`codex exec resume`으로 세션을 잇는 것도 함께 미룬다 — 토큰 절약 최적화이지 기능이
아니고, Codex #2는 채점 결과와 diff만으로 무엇을 바꿨고 결과가 어땠는지 알 수 있다.

## 전역 제약

- **각 Stage 완료 시 다음으로 넘어가기 전에 실험 1건을 끝까지 돌려 회귀를 확인한다.**
- **한 Stage씩만 켠다.** 여러 층을 동시에 켜면 실패 원인이 겹쳐 규명이 불가능하다.
  2026-08-08에 네 겹이 겹쳐 있던 것을 하나씩 걷어내며 배운 원칙이다.
- Stage 1이 끝나면 "숫자가 남는가", Stage 3이 끝나면 "에이전트가 리포트를 쓰는가",
  Stage 2가 끝나면 "통합해도 같은 숫자인가"가 각각 독립적으로 증명되어야 한다.

## Stage 0 — 선행 (다른 저장소·수동)

- [x] **`Autoresearch-infra` 매니페스트에 `ORCH_ACTIVE_DEADLINE_SEC=60000`,
      `ORCH_CODEX_TIMEOUT_SEC=6000` 반영.** 클러스터의 launcher CronJob env에서 두 값
      확인됨(2026-08-09)
- [x] **`autoresearch-experiment-job-contract` ValidatingAdmissionPolicy의 `codex-home`
      허용 목록에 `candidate-finalizer` 추가.** Stage 3이 `codex-home`을
      `candidate-finalizer`에도 mount하면서 필요해졌다. **이 저장소의 릴리스만으로는
      반영되지 않는다** — 정책은 infra 소유이고 admission에서 막히면 Job이 422로
      거부되며 `RUNNING`인 채 매 tick 재시도된다(#640 첫 배포에서 실물로 겪음).
      계약을 바꾸는 PR은 **같은 PR에서 이 정책의 변경 필요 여부를 확인해야 한다**
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

- [x] `agent_orchestration/executor/measurement.py` — `training.py`의 `_run` 패턴을
      따라 `evaluate-model`을 **6회**(2조건 × 3seed) 호출하고 결과 JSON을 수집
- [x] `metrics.json` 조립 (`experiment-metrics-v1`)
      - seed별 baseline·candidate의 전체 held-out 지표
      - paired delta: ROC-AUC · LogLoss · Brier의 seed별 차이 + 평균 + 표준오차.
        **판정하지 않는다 — 수치만 낸다**
      - `split_matches`(두 조건의 테스트셋 SHA-256 대조) · `dataset_fingerprint`
      - 실험 좌표는 호출부가 주입한다
- [x] 신뢰구간은 executor에서 만들지 않는다 — t 임계값 표가
      `src/pipeline/seed_sweep.py`에 있고 복제하면 두 벌이 갈라진다

### 1-3. GCS 게시

- [x] 버킷 `gs://autoresearch-503903-autoresearch-dev-experiment-results`.
      `exp-job` GSA가 `objectCreator`(**교체 불가**)+`objectViewer`만 가짐 — 확인됨
- [x] 경로 규칙 확정: `experiments/{issue_number}/{experiment_id}/...`
- [x] `metrics.json` · 모델 · 테스트셋 · 검증 결과 게시 — `results_store.py`,
      `if_generation_match=0`으로 write-once
- [x] launcher가 버킷 URI를 env로 주입 — `ORCH_EXPERIMENT_RESULTS_ROOT`를
      게시가 일어나는 `candidate-finalizer`에만 준다. `README.md`·
      `agent-project-reference.md` 동반 갱신 완료

### 1-4. 배선

- [x] `phase2.py`에 측정 단계 추가. **현행 8-container 구조 그대로** 붙인다
      (candidate 학습이 있는 `candidate-finalizer` 뒤)
- [ ] `evaluate-model`이 이미지의 어느 python·라이브러리로 도는지 확인한다.
      `train-model`과 같아야 학습·평가가 어긋나지 않는다 (`uv sync`는 workspace를
      대상으로 하고 `_run`은 PATH의 python을 쓴다 — 지금 같은 환경인지 미확인).
      **이미지 정의가 `Autoresearch-infra` 소유라 실물 확인이 필요하다**
- [x] Experiment API `metric_snapshot`에 지표 반영 —
      `POST /internal/executor/experiments/{id}/result`.
      요약 계약은 `experiment-metric-snapshot-v1`이고 전문(`metrics.json`)은 GCS에
      남긴 뒤 `results_uri`로 잇는다. 같은 실험에 다른 숫자를 두 번 쓰지 못한다

### 검증

- [x] 실험 1건이 `metrics.json`을 GCS에 남긴 채 완주 — **#634, 33분 33초**
- [x] `metric_summary`가 `null`이 아니다 — 전문↔요약 대조 9항목 일치
- [x] **같은 `base_dev_sha`로 2건을 돌려 baseline seed별 값이 일치하는지 관측한다.**
      → 아래 "재현성 실측" 참조. **일치했다**
- [x] `uv run python -m pytest`(2516 passed), `uv run --no-sync ruff check ...`

### 재현성 실측 (2026-08-09, #634·#635)

같은 `base_dev_sha`(`8242d3b`)·같은 가설로 두 건을 돌렸다.

**두 조건 × 3seed × 3지표 18개 값이 소수점 아홉 자리까지 일치**했다.
`test_set_sha256`도 6개 전부 동일하고, paired delta와 표준오차까지 같다.

```
roc_auc  baseline 0.627265 → candidate 0.635811   delta +0.008546  se 0.002131
         seed별 delta  +0.012027 / +0.004678 / +0.008933  (3seed 모두 개선)
log_loss 0.362953 → 0.396962   +0.034009   ← 악화
brier    0.116992 → 0.129235   +0.012243   ← 악화
```

**확정된 것 셋**

1. `+0.008546`은 실행 잡음이 아니다. 두 실행이 같은 값을 냈으므로 코드 변경의 효과다
2. 학습은 seed로 완전히 결정적이다. Pod·노드·시각이 달라도 분할까지 동일하다
3. **`baseline` 학습 캐싱의 전제가 충족됐다** — 같은 `base_dev_sha`면 baseline은 항상
   같다. 실험당 약 5분(4분 48초·5분 21초 실측)을 아낄 수 있다

**관측된 사실 하나 더:** 순위 지표는 올랐는데 캘리브레이션 지표 둘이 뚜렷하게 나빠졌다.
요약에 주 지표 하나만 실었다면 "+0.0085 개선"만 보이고 이 손상은 보이지 않았을 것이다
— 세 지표를 모두 싣는 판단이 첫 실험에서 값을 했다. 가설의 성패 판정은 Stage 3의
`report.md`가 할 몫이고, `PASSED`는 "완주하고 결과가 나왔다"는 뜻이다.

### Stage 1 실행에서 드러난 제약 (Stage 1 밖의 원인)

실험 3건 중 2건이 Stage 1과 무관한 곳에서 막혔다. 후속 작업으로 분리한다.

- **stderr 관측 부재 → [#636](https://github.com/SKYAHO/Autoresearch/issues/636).**
  `training._run`·`measurement._run`이 `capture_output=True`로 출력을 잡아둔 뒤
  버려서, 실패 사유 코드만 남고 본문이 사라진다. 주석은 "Pod 로그로 흐른다"고 적혀
  있으나 사실이 아니다. #633 진단에 로컬 재현 20분이 필요했다. **후속 넷 중 첫
  번째여야 한다** — 나머지 셋이 모두 "다음 실패의 원인을 알 수 있는가"에 기댄다
- **ONNX 재귀 제약.** `train-model`은 학습과 서빙 ONNX 패키징을 한 덩어리로 한다.
  #633에서 `num_leaves` 31→63 변경이 학습·검증·모델 저장을 다 통과하고
  `convert_lgbm_to_onnx`의 트리 파서 재귀에서 `RecursionError`로 죽었다. 로컬 재현으로
  base(31)는 성공, candidate(63)만 실패함을 확인했다.
  → 학습 실패와 서빙 패키징 실패를 분리하는 쪽을 권한다. 실험의 목적은 지표를 재는
  것이고 ONNX는 서빙 산출물이다. 패키징 실패로 측정을 통째로 잃는 것은 손해다.
  분리하지 않는다면 **Stage 3 하네스 지침에 제약을 명시**해야 한다 — 지금은 에이전트가
  알 방법이 없고, 트리 크기 조정은 가장 자연스러운 첫 실험 아이디어 중 하나다
- **Codex 자격증명이 구조적으로 일회성이다.** #632가 `codex-worker`에서 32초 만에
  죽었다. `_prepare_runtime_codex_home`이 `auth.json` 복사본을 `0400`으로 만들어 갱신본을
  쓸 수 없고, 설령 쓰더라도 `TemporaryDirectory`라 사라진다. ChatGPT OAuth는 refresh
  token을 쓸 때마다 교체하므로 **access token 만료 시 한 번 쓰이고 영구히 죽는다.**
  Secret은 08-07 발급 후 무갱신이었고 08-08의 #619는 통과, 08-09의 #632는 실패로 앞뒤가
  맞는다. 구독제라 API key 전환은 불가.
  → 복사본 모드를 `0600`으로 바꾸고 **갱신본을 Secret에 되쓴다.** 사용자 판단으로
  되쓰기는 같은 컨테이너에서 하고, Codex의 자격증명 접근 금지는 하네스 지침이 담당한다
  (verifier의 정책 강제를 하네스로 대체한 결정과 같은 논리). RBAC는 해당 Secret 하나에
  `patch`만 준다. 착수 전에 **모드를 풀면 Codex가 실제로 `auth.json`을 갱신해 쓰는지**
  부터 관측한다

## Stage 2 — 컨테이너 8 → 4 (**Stage 3 뒤로 미룸**)

**목표: 통합해도 같은 숫자가 나온다.** Stage 1이 있어야 이 회귀를 확인할 수 있다.
착수는 Stage 3의 `report.md`가 실물로 확인된 뒤다 — 위 "실행 순서" 참조.

- [ ] **착수 전 결정: prepare를 ②에 흡수할지 (4 vs 5 컨테이너).** spec 결정 3의
      credential 지도가 자기모순이다 — ②는 clone 토큰이 필요한데 같은 표가 ②에
      GitHub 자격증명이 없다고 적었고, verifier의 credential 검사 제거가 그 전제에
      기대고 있다. 선택지와 근거는 spec 결정 3의 "미결" 블록 참조. **아래 항목들은
      어느 쪽을 골라도 그대로 유효하다** — 흡수 여부와 별개다
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

## Stage 3 — AGENTS.md + Codex 2회 + `report.md` (**Stage 2보다 먼저**)

**목표: 에이전트가 자기 실험 결과로 리포트를 쓴다.** 컨테이너 재구성 없이 간다(#639).

### 3-1. AGENTS.md 교체 (현행 버그 수정)

- [x] executor 전용 하네스 지침 작성 — 실험 하네스가 무엇인지, 산출물이 무엇인지,
      **작업 범위(경로)**, 저장소 기여 가이드가 적용되지 않는 것들.
      `executor/prompt.py`의 `build_harness_instructions()`가 소유한다
- [x] **ONNX 재귀 제약을 지침에 명시** — 트리 크기를 키우는 하이퍼파라미터는
      `convert_lgbm_to_onnx`에서 `RecursionError`로 죽는다(#633 실측, spec 6번).
      에이전트는 알 방법이 없고 트리 크기 조정은 가장 자연스러운 첫 실험 아이디어다
- [x] `codex-worker`가 Codex 실행 **직전에** 교체하고 `finally`로 **원본 복원**.
      verifier가 `git status`·`ls-files --others`로 변경을 수집하므로, 되돌리지 않으면
      하네스 파일이 candidate 변경으로 잡혀 commit·push된다
- [ ] `prompt.py`의 허용·금지 경로와 지시문을 이 파일로 **이관** — 지금은 두 곳이 같은
      목록을 공유(`_allowed_paths`)할 뿐 프롬프트에서 걷어내지는 않았다
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

> 위 세 항목은 **Stage 2와 함께** 처리한다. 셋 다 verifier·prompt 계약을 건드리므로
> `report.md`를 실물로 확인하기 전에 겹칠 이유가 없다(전역 제약: 한 Stage씩만 켠다).

### 3-2. Codex 2회 호출

- [x] `phase2.candidate_finalizer_main`이 채점 뒤 Codex #2를 호출한다.
      출력은 `<workspace>/result/report.md` — clone 밖, `metrics.json`과 같은 자리
- [x] `codex_worker`가 프롬프트를 주입받는 실행 경로(`run_codex_execution`)를 연다.
      `.git` 봉인과 하네스 교체는 코드 수정 실행에만 붙는다 — 리포트는 clone 밖에서 돈다
- [x] **Codex #2가 실패해도 게시와 API 보고는 그대로 일어난다.** 리포트가 없다고
      숫자까지 잃으면 손해다
- [x] `report.md` 절 구성 확인 → 빠진 절은 **로그로만** 남기고 리포트는 버리지 않는다.
      형식이 어긋난 리포트가 리포트 없음보다 낫다
- [ ] `--ephemeral` 제거 + 같은 `CODEX_HOME` 공유 + `codex exec resume --last`.
      **MVP 범위 밖으로 미룸** — 토큰 절약 최적화이지 기능이 아니다
- [ ] `--output-schema <FILE>`·`-o`로 최종 응답 수신. 리포트를 파일로 직접 쓰게 했으므로
      MVP에서는 필요 없다. stdout tail 스크래핑 대체는 후속

### 3-3. `report.md` 내용 계약

- [x] 가설 / 무엇을 어떻게 바꿨는지 / before-after 주 지표 / 보조 지표 /
      seed별 표 / 데이터·분할 provenance / 에이전트의 결론과 근거.
      절 목록의 정본은 `prompt.REPORT_SECTIONS`이고, 프롬프트의 지시와 산출물 확인이
      같은 목록을 본다

### 3-4. 배선

- [x] launcher가 `candidate-finalizer`에 Codex 인증 Secret·`ORCH_CODEX_HOME`·
      `ORCH_CODEX_TIMEOUT_SEC`을 준다. `ORCH_CODEX_HOME`이 없으면 리포트를 켜지 않은
      배포로 읽고 사유를 남긴 뒤 건너뛴다
- [x] 게시를 **두 번에 나눈다** — 채점 직후 `metrics.json`·학습 산출물,
      Codex #2 뒤 `report.md`. 한 번에 올리면 Codex 실행 시간만큼 숫자를 잃을 수 있는
      창이 열린다(container가 죽으면 잡을 예외가 없다)
- [x] 산출물이 **regular file인지 코드로 확인**한다 — symlink는 게시가 링크 대상을
      그대로 올린다. 하네스 파일 교체·복원도 링크를 지우고 `O_CREAT | O_EXCL`로만 쓴다
- [x] 리포트 지시문에 credential 규칙을 넣는다 — 하네스는 clone 루트에 심기므로
      clone 밖에서 도는 Codex #2에는 닿지 않는다
- [ ] **NetworkPolicy 확인 불필요** — Codex #2는 같은 Pod 안이라 기존 egress 규칙을
      그대로 쓴다. infra 변경 없음

### 검증

- [x] `report.md`가 GCS에 남고, 그 안의 숫자가 `metrics.json`과 일치 — **#644**
- [x] AGENTS.md 충돌로 인한 `no_changes`가 재발하지 않음 — #641·#644 모두 통과
- [x] 하네스 교체가 candidate diff에 나타나지 않는다 — 커밋에 `AGENTS.md` 없음
- [x] pytest·ruff

### 실측 (2026-08-10, #641·#644)

**Stage 3 완료.** 실험 `889b3e3c…`(이슈 #644)가 약 37분에 완주하며 리포트를 남겼다.

| 확인 | 결과 |
|---|---|
| `report.md` 게시 | `gs://…/experiments/644/889b3e3c…/report.md` (5,416 B) |
| 절 구성 | `sections_missing=0` — 계약이 요구한 7개 전부 |
| 숫자 일치 | 리포트의 소수·정수 **50개 전부** `metrics.json`과 일치, 불일치 0 |
| 지문·좌표 | SHA-256 4개, 좌표 4개, `dataset_fingerprint` 모두 일치 |
| candidate 커밋 | `9c64643` — `lgbm_model.py`·`config.yaml` 두 개뿐 |
| 상태 | `PASSED`, `metric_summary` 채워짐 |
| 재현성 | 4회째 일치 (#634·#635·#641·#644) |

**로그 순서가 설계대로 나왔다.** `experiment results published` → Codex #2 →
`experiment report published`. 숫자가 먼저 확정된다.

**두 번 걸렸고 두 번 다 순서 결정이 막아 줬다.**

- **#641** — Codex CLI가 clone 밖 디렉터리에서 실행을 거부했다
  (`Not inside a trusted directory and --skip-git-repo-check was not specified`).
  리포트는 없었지만 `metrics.json`은 이미 게시된 뒤라 실험은 `PASSED`로 끝났다.
  게시를 리포트 뒤에 뒀다면 31분을 쓰고 아무것도 남기지 못했을 것이다.
  수정: [#642](https://github.com/SKYAHO/Autoresearch/issues/642) →
  [#643](https://github.com/SKYAHO/Autoresearch/pull/643)
- 단위 테스트가 이것을 못 잡은 이유는 **가짜 `codex` executable에 git repository
  검사가 없기 때문**이다. 실물 CLI의 통합 속성이라 실험을 띄워야 드러난다.
  같은 종류를 다시 놓치지 않도록 argv 계약을 양방향으로 고정했다(리포트 실행에는
  플래그가 있고, 코드 수정 실행에는 없다)

**배포 좌표:** executor `sha256:f9a73d1e…`(v0.12.1), launcher `sha256:f463fd30…`.
`candidate-finalizer`가 `codex-home`을 mount하게 되면서
`autoresearch-experiment-job-contract` ValidatingAdmissionPolicy도 함께 바꿔야 했다
(`codex-home` 허용 목록에 `candidate-finalizer` 추가). **infra 소유이며 이 저장소의
릴리스만으로는 반영되지 않는다** — #640 첫 배포에서 실험이 422로 막혀 드러났다.

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

- [x] `PASSED` = 실험이 완주하고 결과가 나왔다. **결과 보고 endpoint가 상태를 인자로
      받지 않아** 호출자가 도달할 상태를 고를 수 없다
- [x] 가설의 성패는 `report.md`가 서술. 지표는 `metric_snapshot`으로 워크벤치 노출
- [x] 이 경로 전용 결과 계약 정의 — `ExecutorResultReportRequest` /
      `experiment-metric-snapshot-v1`. `PairedExperimentResult`를 쓰지 않으므로
      `ConditionLineage` 등 격리 실행 모델 필드를 채울 일이 없다
- [x] **실행 실패는 `FAILED`가 아니라 `ERROR`다.** executor가 죽으면 스스로 보고할 수
      없으므로 launcher의 Job 회수가 처리한다. 죽는 쪽이 자기 죽음을 보고하는 경로를
      신뢰하지 않는다. `reconcile_failed_jobs`가 `RUNNING`뿐 아니라 `EVALUATING`도
      회수한다 — candidate 보고 **뒤에** 학습·채점·게시·보고가 오기 때문이다.
      결과 보고까지 끝난 `PASSED`는 회수하지 않는다
- [ ] `FAILED`는 이 경로에서 쓰이지 않는다. 상태 자체를 없앨지는 Stage 3 이후 결정

## MVP 범위 밖

**MVP는 #644로 닫혔다.** 아래는 그 뒤에 다룬다. 순서는 정하지 않았고, 착수 시점만
남았다.

- Claude 리뷰어의 **거부권** — 판정이 숫자와 일관되게 움직이는지 실적이 쌓인 뒤
- **baseline 학습 캐싱** — ~~Stage 1 검증에서 재현성이 확인된 뒤~~ **전제 충족.**
  #634·#635·#641·#644 네 건이 완전히 일치했으므로 같은 `base_dev_sha`의 baseline은
  재사용할 수 있다. 실험당 약 5분
- `tests/**` 쓰기 권한 회수 / 하네스 격리 디렉터리로의 산출물 전환
- 피처 변경 실험 해금 (`feature_change_unsupported`)
- `CREATED → ERROR` 전이
- `POLICY_SEEDS` 상향 — `ORCH_ACTIVE_DEADLINE_SEC` 60000이면 여유가 생겼다
- **워크벤치에서 `report.md`를 읽는다.** UI는 지금 요약 지표와 `results_uri`까지만
  싣는다. 실험의 최종 산출물이 리포트인데 사람이 그것을 보려면 `gsutil`을 써야 한다
- **`test_verify_comparison_rechecks_receipts_and_records_verified_metrics`의
  시간 의존 flake.** `metric receipt 시간이 run … 범위 밖입니다`로 드물게 실패한다
  (2026-08-09 CI 1회). 영수증 시각은 마이크로초인데 MLflow run 시각은 밀리초로
  잘리고, 실측 여유가 **약 5ms**뿐이라 부하 높은 러너에서 뒤집힌다. 테스트가
  실제 시계에 기대는 것이 원인이므로 `next_metric_time_created` 훅으로 시각을
  주입하는 쪽이 근본 수정이다. **`src/pipeline/` 소유라 이 계획의 범위 밖이다**

### 이미 이슈가 있는 후속

- **stderr 관측 부재** — [#636](https://github.com/SKYAHO/Autoresearch/issues/636).
  `training._run`·`measurement._run`이 subprocess 출력을 버려 실패 사유 코드만 남는다.
  후속 넷 중 **첫 번째여야 한다** — 나머지가 모두 "다음 실패의 원인을 알 수 있는가"에
  기댄다
- **ONNX 재귀 제약의 코드 분리** — 지금은 하네스 지침 한 줄로 막고 있다(Stage 3-1).
  학습 실패와 서빙 패키징 실패를 분리하는 것이 근본 수정이다
- **Codex 자격증명 되쓰기** — spec §7. access token 만료 시 refresh token이 한 번
  쓰이고 영구히 죽는다
