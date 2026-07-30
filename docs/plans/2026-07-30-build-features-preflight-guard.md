# build-features 사전 점검 가드 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `build-features`(`src/pipeline/build_training_dataset.py`)가 필수 환경변수·
feast 패키지·GCP 자격증명을 BigQuery 접속 전에 확인해, 설정이 없을 때 응답 없이
멈추는 대신 즉시 명확한 이유로 실패하게 만든다.

**Architecture:** `_verify_assembly_environment()`를 신설해 `main()`의 날짜 검증
직후, `_assemble_via_feast()` 호출 전에 실행한다. 체크 순서는 환경변수 → feast
import → GCP 자격증명(가장 저렴한 것부터). GKE는 `KUBERNETES_SERVICE_HOST`로
감지해 자격증명 파일 체크를 건너뛴다.

**Tech Stack:** Python 3.11/3.12, 표준 라이브러리(`os`)만 사용. 신규 외부 의존성 없음.

## Global Constraints

- 기존 happy path(환경이 올바르게 설정된 경우의 `build-features` 동작)는 바뀌지
  않는다 — 이 가드는 실패 시점만 앞당긴다.
- `_assemble_via_feast()`, `load_training_entity_spine()`, `src/cli.py`의 다른
  커맨드는 이 플랜에서 수정하지 않는다.
- 새 함수의 예외는 이 모듈의 기존 스타일과 동일하게 `ValueError`를 쓴다
  (`main()`의 날짜 검증이 이미 그렇게 한다).
- 커밋 메시지 형식은 `<type>: <한국어 설명>` (`.claude/docs/agent-workflow-reference.md`).
- 관련 설계 문서: `docs/specs/2026-07-30-build-features-preflight-guard.md`.
  관련 이슈: #404.

---

### Task 1: `_verify_assembly_environment()` 구현 + main() 통합 + dev 그룹 테스트

**Files:**
- Modify: `src/pipeline/build_training_dataset.py`
- Test: `tests/test_build_training_dataset.py`

**Interfaces:**
- Produces: `src.pipeline.build_training_dataset._verify_assembly_environment() -> None`
  — 통과하면 아무것도 반환하지 않음, 실패 조건마다 `ValueError`.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_build_training_dataset.py` 끝에 추가한다(파일 상단에 이미
`import os`, `from src.pipeline import build_training_dataset`가 있다):

```python
def test_verify_assembly_environment_requires_registry_path(monkeypatch) -> None:
    monkeypatch.delenv("GCS_REGISTRY_PATH", raising=False)
    monkeypatch.setenv("GCS_STAGING_LOCATION", "gs://staging/")
    try:
        build_training_dataset._verify_assembly_environment()
        raised = False
    except ValueError as error:
        raised = True
        assert "GCS_REGISTRY_PATH" in str(error)
    assert raised


def test_verify_assembly_environment_requires_staging_location(monkeypatch) -> None:
    monkeypatch.setenv("GCS_REGISTRY_PATH", "gs://registry/registry.db")
    monkeypatch.delenv("GCS_STAGING_LOCATION", raising=False)
    try:
        build_training_dataset._verify_assembly_environment()
        raised = False
    except ValueError as error:
        raised = True
        assert "GCS_STAGING_LOCATION" in str(error)
    assert raised


def test_verify_assembly_environment_requires_feast_package(monkeypatch) -> None:
    # 이 저장소 dev 그룹에는 실제로 feast가 설치돼 있지 않다(격리 그룹) — mocking 없이
    # 실제 ImportError를 검증한다. feast 그룹에서 이 테스트를 돌리면 통과하지 않으므로
    # dev 그룹 전용이다.
    monkeypatch.setenv("GCS_REGISTRY_PATH", "gs://registry/registry.db")
    monkeypatch.setenv("GCS_STAGING_LOCATION", "gs://staging/")
    try:
        build_training_dataset._verify_assembly_environment()
        raised = False
    except ValueError as error:
        raised = True
        assert "feast" in str(error)
    assert raised


def test_main_env_check_runs_before_bigquery_call(monkeypatch) -> None:
    # 회귀 테스트: 원래 버그(환경변수 확인보다 BigQuery 호출이 먼저 실행됨)를 잡는다.
    # 필수 환경변수를 비우고 main()을 호출했을 때 load_training_entity_spine이
    # 아예 호출되지 않는지 확인한다(호출되면 AssertionError로 즉시 실패).
    monkeypatch.delenv("GCS_REGISTRY_PATH", raising=False)
    monkeypatch.delenv("GCS_STAGING_LOCATION", raising=False)

    def _should_not_be_called(*args, **kwargs):
        raise AssertionError("load_training_entity_spine이 호출되면 안 됩니다 — "
                              "환경변수 확인이 먼저 실행돼야 합니다.")

    monkeypatch.setattr(
        build_training_dataset, "load_training_entity_spine", _should_not_be_called
    )

    try:
        build_training_dataset.main(
            events_start_date="2026-07-01", events_end_date="2026-07-01"
        )
        raised = False
    except ValueError as error:
        raised = True
        assert "GCS_REGISTRY_PATH" in str(error)
    assert raised
```

- [ ] **Step 2: 테스트 실행해서 실패 확인**

Run: `uv run python -m pytest tests/test_build_training_dataset.py -k verify_assembly_environment -v`
Expected: FAIL — `AttributeError: module 'src.pipeline.build_training_dataset' has no attribute '_verify_assembly_environment'`

- [ ] **Step 3: `_verify_assembly_environment()` 구현**

`src/pipeline/build_training_dataset.py`의 `_assemble_via_feast` 함수 **바로 위**에
추가한다:

```python
def _verify_assembly_environment() -> None:
    """feast 조립에 필요한 환경을 BigQuery 접속 전에 확인한다(#404/#423).

    순서가 중요하다 — BigQuery 클라이언트 생성(load_training_entity_spine)보다
    먼저 실행돼야, 자격증명 없는 환경에서 응답 없이 멈추는 대신(#396/#423 실측)
    즉시 명확한 이유와 함께 실패한다. 검사는 가장 빠른 것부터: 환경변수 →
    feast import → GCP 자격증명.
    """
    missing_env = [
        name for name in ("GCS_REGISTRY_PATH", "GCS_STAGING_LOCATION")
        if not os.environ.get(name)
    ]
    if missing_env:
        raise ValueError(
            f"{', '.join(missing_env)} 환경변수가 필요합니다. .env.example을 참고해 설정하세요."
        )

    try:
        import feast  # noqa: F401
    except ImportError as error:
        raise ValueError(
            "feast 패키지가 설치되어 있지 않습니다. dev 그룹과 의존성 충돌로 "
            "격리 그룹입니다 — `uv sync --only-group feast`로 설치하세요."
        ) from error

    # GKE 등 컨테이너 환경은 Workload Identity(metadata server)로 인증하므로
    # 로컬 자격증명 파일이 없어도 정상이다(docs/guides/training-image.md,
    # deploy/feast/apply-job.yaml 확인). KUBERNETES_SERVICE_HOST(모든 k8s pod에
    # 자동 존재)가 있으면 이 체크를 건너뛴다.
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        return

    adc_path = os.path.expanduser("~/.config/gcloud/application_default_credentials.json")
    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") and not os.path.exists(adc_path):
        raise ValueError(
            "GCP 자격증명이 감지되지 않습니다 — BigQuery 접속이 응답 없이 멈출 수 "
            "있습니다(#396/#423 실측). `gcloud auth application-default login`을 "
            "실행하거나 GOOGLE_APPLICATION_CREDENTIALS를 설정하세요."
        )
```

- [ ] **Step 4: `main()`에서 호출**

`src/pipeline/build_training_dataset.py`의 `main()` 함수(파일 끝 부분)를 수정한다:

```python
# 변경 전
    if not events_start_date or not events_end_date:
        raise ValueError(
            "events_start_date/events_end_date가 필요합니다 "
            "(spine=training_entity를 BQ에서 KST 날짜 폐구간으로 조회한다)"
        )
    if output_path is None:
        output_path = os.path.join(get_data_dir(), "processed", "training_dataset.csv")
    _assemble_via_feast(output_path, events_start_date, events_end_date)

# 변경 후
    if not events_start_date or not events_end_date:
        raise ValueError(
            "events_start_date/events_end_date가 필요합니다 "
            "(spine=training_entity를 BQ에서 KST 날짜 폐구간으로 조회한다)"
        )
    _verify_assembly_environment()
    if output_path is None:
        output_path = os.path.join(get_data_dir(), "processed", "training_dataset.csv")
    _assemble_via_feast(output_path, events_start_date, events_end_date)
```

- [ ] **Step 5: 테스트 실행해서 통과 확인**

Run: `uv run python -m pytest tests/test_build_training_dataset.py -v`
Expected: PASS 전체(기존 테스트 포함, 신규 4건 포함)

- [ ] **Step 6: 기존 feast 경로 테스트(dev 그룹) 회귀 확인**

Run: `uv run python -m pytest tests/test_build_training_dataset_feast_path.py -v`
Expected: PASS 전체 — 이 파일의 `test_main_requires_event_dates`처럼 환경변수를
직접 세팅하지 않는 기존 테스트가 있다면, 이번 가드 때문에 새로 실패하지 않는지
확인한다(실패한다면 해당 테스트에 `monkeypatch.setenv`로 필수 환경변수를 추가해야
할 수 있다 — 발견 시 이 스텝에서 바로 잡는다).

- [ ] **Step 7: lint 확인**

Run: `uv run --no-sync ruff check src/pipeline/build_training_dataset.py tests/test_build_training_dataset.py`
Expected: `All checks passed!`

- [ ] **Step 8: 커밋**

```bash
git add src/pipeline/build_training_dataset.py tests/test_build_training_dataset.py
git commit -m "feat: build-features에 사전 점검 fail-fast 가드 추가 (#404)"
```

---

### Task 2: feast 그룹 테스트(자격증명 체크) + CI 목록 추가

**Files:**
- Create: `tests/test_build_training_dataset_env_check_feast.py`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: `src.pipeline.build_training_dataset._verify_assembly_environment()` (Task 1)

- [ ] **Step 1: 실패하는 테스트 작성**

```python
"""_verify_assembly_environment()의 GCP 자격증명 체크 — feast 설치가 필요한 부분만
(#404). 환경변수/feast import 체크는 tests/test_build_training_dataset.py(dev
그룹)가 이미 커버한다.
"""

import pytest

pytest.importorskip("feast")

from src.pipeline import build_training_dataset  # noqa: E402

_REQUIRED_ENV = {
    "GCS_REGISTRY_PATH": "gs://registry/registry.db",
    "GCS_STAGING_LOCATION": "gs://staging/",
}


def _set_required_env(monkeypatch) -> None:
    for name, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)


def test_verify_assembly_environment_requires_gcp_credentials(monkeypatch, tmp_path) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    fake_home = tmp_path / "no_adc_here"
    fake_home.mkdir()
    monkeypatch.setattr(build_training_dataset.os.path, "expanduser", lambda p: str(fake_home / "adc.json"))

    with pytest.raises(ValueError, match="자격증명"):
        build_training_dataset._verify_assembly_environment()


def test_verify_assembly_environment_skips_credential_check_on_kubernetes(monkeypatch, tmp_path) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.0.0.1")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    fake_home = tmp_path / "no_adc_here"
    fake_home.mkdir()
    monkeypatch.setattr(build_training_dataset.os.path, "expanduser", lambda p: str(fake_home / "adc.json"))

    build_training_dataset._verify_assembly_environment()  # 예외 없이 통과해야 한다


def test_verify_assembly_environment_passes_with_google_application_credentials(monkeypatch, tmp_path) -> None:
    _set_required_env(monkeypatch)
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    cred_file = tmp_path / "service-account.json"
    cred_file.write_text("{}")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(cred_file))

    build_training_dataset._verify_assembly_environment()  # 예외 없이 통과해야 한다
```

- [ ] **Step 2: feast 그룹 환경에서 테스트 실행해서 실패 확인**

Run: `uv sync --only-group feast` (아직 안 했다면), 이어서
`uv run python -m pytest tests/test_build_training_dataset_env_check_feast.py -v`
Expected: 첫 번째 테스트는 Task 1이 이미 구현했으므로 실제로는 **PASS할 수 있다**
— 이 태스크의 진짜 신규 산출물은 테스트 파일 자체와 CI 배선이다. 만약 세 테스트가
이미 모두 PASS라면 Step 3(구현)은 건너뛰고 바로 Step 4(CI 배선)로 간다.

- [ ] **Step 3: 실패하는 테스트가 있다면 `_verify_assembly_environment()` 조정**

Task 1의 구현이 설계대로라면 이 스텝은 필요 없다. 실패하는 테스트가 있다면
`src/pipeline/build_training_dataset.py`의 `_verify_assembly_environment()`를
테스트가 요구하는 대로 조정한다(단, Task 1의 Global Constraints를 어기지 않는
범위에서).

- [ ] **Step 4: CI 워크플로우에 새 테스트 파일 추가**

`.github/workflows/ci.yml`의 `pytest-feast` job "Run feast pytest" 스텝이 실행하는
파일 목록(정확한 현재 내용은 직접 파일을 열어 확인할 것 — 아래는 이 플랜 작성
시점 기준)에 `tests/test_feast_retrieval_integration_feast.py` 다음 줄로 한 줄
추가:

```yaml
      - name: Run feast pytest
        run: >-
          uv run --no-sync python -m pytest
          tests/test_redis_iam.py
          tests/test_feast_materialize.py
          tests/test_serving_feast_reader.py
          tests/test_serving_feast_reader_feast.py
          tests/test_offline_retrieval_smoke_feast.py
          tests/test_odfv_category_match_feast.py
          tests/test_odfv_registry_portability_feast.py
          tests/test_verify_registry_portability_feast.py
          tests/test_feast_retrieval_integration_feast.py
          tests/test_build_training_dataset_env_check_feast.py
          tests/test_serving_api.py
```

(즉 기존 목록 순서를 유지하고, `test_feast_retrieval_integration_feast.py`와
`test_serving_api.py` 사이에 새 파일 한 줄만 끼워 넣는다.)

- [ ] **Step 5: 테스트 실행해서 통과 확인**

Run: `uv run python -m pytest tests/test_build_training_dataset_env_check_feast.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: `git diff --check`로 워크플로우 파일 검증**

Run: `git diff --check`
Expected: 출력 없음(trailing whitespace 등 없음)

- [ ] **Step 7: lint 확인**

Run: `uv run --no-sync ruff check tests/test_build_training_dataset_env_check_feast.py`
Expected: `All checks passed!`

- [ ] **Step 8: 커밋**

```bash
git add tests/test_build_training_dataset_env_check_feast.py .github/workflows/ci.yml
git commit -m "test: build-features 자격증명 사전점검 feast 그룹 테스트 추가 (#404)"
```

---

## Self-Review 체크리스트 (구현 착수 전 확인 완료)

- **Spec coverage**: 설계 문서의 완료 조건 4개 중 첫 3개(기존 경로 유지, fail-fast
  가드, GKE 오탐 방지)는 Task 1·2가 구현·검증한다. 4번째(champion 절차 문서화)는
  설계 문서 자체로 이미 충족돼 있어 별도 코드 태스크가 필요 없다.
- **Placeholder scan**: 없음 — 모든 스텝에 실제 코드/명령이 있다.
- **Type consistency**: `_verify_assembly_environment()` 시그니처(인자 없음, 반환
  없음, `ValueError` 발생)가 Task 1·2 전체에서 동일하게 쓰인다.

## 다음 단계 (이 플랜 범위 밖)

두 태스크가 끝나고 전체 테스트가 통과하면(dev 그룹 전체 + feast 그룹),
`.claude/docs/agent-workflow-reference.md`의 PR 워크플로우로 리뷰를 요청한다.
