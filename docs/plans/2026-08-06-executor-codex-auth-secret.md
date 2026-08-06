# Executor Codex Auth Secret Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Experiment Executor의 Codex 인증 원본을 RWO PVC에서 Kubernetes Secret으로 바꿔 서로 다른 노드의 여러 Experiment Pod가 동시에 시작될 수 있게 한다.

**Architecture:** launcher는 `ORCH_CODEX_HOME_SECRET_NAME`을 필수 설정으로 읽고, Phase 2 Job의 `codex-home` volume을 `auth.json` key 하나만 제공하는 Secret volume으로 만든다. `codex-worker`만 이 volume을 `/var/lib/codex`에 read-only mount하며 기존 per-run scratch 복사 동작은 유지한다.

**Tech Stack:** Python 3.12, Kubernetes Python client, pytest, Ruff.

## Global Constraints

- 구현 정본은 `docs/specs/2026-08-06-experiment-executor-phase2.md`의 “Codex 인증 동시 mount 계약 (#565)”이다.
- 설정 이름은 `ORCH_CODEX_HOME_SECRET_NAME`, dataclass 필드는 `codex_home_secret_name`으로 고정한다.
- Secret key와 mount 파일 이름은 모두 `auth.json`, Secret volume `defaultMode`는 `0440`이다.
- `codex-worker`만 `codex-home` volume을 `/var/lib/codex`에 read-only mount한다.
- 기존 Runner의 `agent-orchestration-codex-home` PVC와 Runner 문서는 변경하지 않는다.
- Secret 값이나 실제 `auth.json`은 테스트 fixture, 문서, Git diff에 포함하지 않는다.
- 코드 변경은 실패 테스트 작성 → 예상 실패 확인 → 최소 구현 → 관련 테스트 통과 → 전체 검증 순서로 수행한다.

---

### Task 1: Launcher의 Codex 인증 volume을 Secret으로 전환

**Files:**
- Modify: `tests/test_experiment_launcher.py`
- Modify: `tests/test_experiment_executor_integration.py`
- Modify: `agent_orchestration/launcher/config.py`
- Modify: `agent_orchestration/launcher/jobs.py`
- Modify: `.env.example`
- Modify: `README.md`
- Modify: `.claude/docs/agent-project-reference.md`
- Modify: `docs/plans/2026-08-06-experiment-executor-phase2.md`

**Interfaces:**
- Consumes: Infra가 experiment namespace에 생성하는 Codex 인증 Secret 이름.
- Produces: `LauncherSettings.codex_home_secret_name: str`, 환경 변수 `ORCH_CODEX_HOME_SECRET_NAME`, `V1SecretVolumeSource(secret_name=..., items=[auth.json], default_mode=0o440)`.

- [ ] **Step 1: Secret 기반 설정과 manifest의 실패 테스트 작성**

  `tests/test_experiment_launcher.py`의 `_settings()`와 환경 변수 fixture를 `codex_home_secret_name="codex-auth"`, `ORCH_CODEX_HOME_SECRET_NAME=codex-auth`로 바꾼다. 환경 로딩 결과가 `settings.codex_home_secret_name == "codex-auth"`인지 단언한다.

  `tests/test_experiment_executor_integration.py`의 manifest 계약 테스트에서 실제 `build_executor_job()` 결과를 다음 literal 값으로 검사한다.

  ```python
  codex_volume = next(volume for volume in pod.volumes if volume.name == "codex-home")
  assert codex_volume.persistent_volume_claim is None
  assert codex_volume.secret.secret_name == "codex-auth"
  assert codex_volume.secret.default_mode == 0o440
  assert [(item.key, item.path) for item in codex_volume.secret.items] == [
      ("auth.json", "auth.json")
  ]
  assert {
      container.name
      for container in [*pod.init_containers, *pod.containers]
      if any(mount.name == "codex-home" for mount in container.volume_mounts)
  } == {"codex-worker"}
  ```

- [ ] **Step 2: RED 확인**

  실행:

  ```bash
  uv run --no-sync python -m pytest \
    tests/test_experiment_launcher.py \
    tests/test_experiment_executor_integration.py -q
  ```

  기대: `LauncherSettings`가 `codex_home_secret_name`을 받지 못하거나 manifest의 `codex-home`이 여전히 PVC라서 실패한다.

- [ ] **Step 3: 최소 구현**

  `agent_orchestration/launcher/config.py`에서 모듈 docstring의 `PVC`를 `Secret`으로 바꾸고 다음 이름을 일관되게 교체한다.

  ```python
  codex_home_secret_name: str
  # required_strings key도 codex_home_secret_name
  codex_home_secret_name=_required_environment("ORCH_CODEX_HOME_SECRET_NAME")
  ```

  `agent_orchestration/launcher/jobs.py`에서 `V1PersistentVolumeClaimVolumeSource` import와 사용을 제거하고 `codex-home` volume을 다음과 같이 만든다.

  ```python
  V1Volume(
      name="codex-home",
      secret=V1SecretVolumeSource(
          secret_name=settings.codex_home_secret_name,
          items=[V1KeyToPath(key="auth.json", path="auth.json")],
          default_mode=0o440,
      ),
  )
  ```

  기존 `codex-worker`의 read-only mount와 다른 컨테이너의 mount 부재는 변경하지 않는다.

- [ ] **Step 4: GREEN 및 회귀 테스트 확인**

  실행:

  ```bash
  uv run --no-sync python -m pytest \
    tests/test_experiment_launcher.py \
    tests/test_experiment_executor_integration.py \
    tests/test_experiment_codex_worker.py -q
  ```

  기대: 전체 통과.

- [ ] **Step 5: 환경·운영 문서 계약 갱신**

  `.env.example`은 `ORCH_CODEX_HOME_PVC_NAME`을 `ORCH_CODEX_HOME_SECRET_NAME`으로 바꾸고 값은 비워 둔다. README, project reference와 기존 Phase 2 plan은 executor 인증 원본이 전용 Secret이며 실제 이름은 Infra가 소유한다고 명시한다. Runner OAuth PVC 설명은 유지한다.

- [ ] **Step 6: 전체 검증**

  실행:

  ```bash
  uv run python -m pytest
  uv run --no-sync ruff check agent_orchestration autoresearch tests tools
  git diff --check
  ```

  기대: pytest, Ruff, diff check 모두 통과하고 Git diff에 Secret 값·`auth.json` 파일·무관한 변경이 없다.

- [ ] **Step 7: 커밋**

  ```bash
  git add agent_orchestration/launcher/config.py \
    agent_orchestration/launcher/jobs.py \
    tests/test_experiment_launcher.py \
    tests/test_experiment_executor_integration.py \
    .env.example README.md .claude/docs/agent-project-reference.md \
    docs/specs/2026-08-06-experiment-executor-phase2.md \
    docs/plans/2026-08-06-experiment-executor-phase2.md \
    docs/plans/2026-08-06-executor-codex-auth-secret.md
  git commit -m "fix: executor Codex 인증을 Secret으로 전환한다"
  ```
