# 모델 승격 구조화 결과 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `promote-model`이 승격·게이트 미달·후보 없음·실행 오류를 버전이 있는 JSON 결과로 구분하고 Airflow XCom 파일에 안전하게 기록하게 한다.

**Architecture:** 판정 본체는 Pydantic 결과 모델을 반환하고 정책 미달을 예외가 아닌 정상 결과로 표현한다. CLI는 명시적 `model-promotion-result-v1` opt-in에서 JSON stdout과 원자적 결과 파일을 제공하며, 인자를 보내지 않는 기존 Airflow 호출에는 legacy exit code를 유지한다.

**Tech Stack:** Python 3.11/3.12, Typer, Pydantic v2, MLflow client, pytest, ruff

## Global Constraints

- 구조화 결과 contract 값은 정확히 `model-promotion-result-v1`이다.
- `promoted`, `rejected`, `no_candidate`는 구조화 모드에서 exit 0이고 `error`는 exit 1이다.
- `--result-contract`와 `--result-path`는 함께만 허용하며 잘못된 조합은 판정 전 exit 2다.
- 구조화 인자를 생략한 legacy 호출은 `rejected`를 exit 1로 유지한다.
- stdout의 구조화 결과는 한 줄 JSON이고 결과 파일은 같은 JSON object다.
- 결과에는 원본 예외, traceback, credential, URI userinfo, MLflow run ID를 넣지 않는다.
- Airflow, Slack webhook, DAG 배선은 이 저장소에서 변경하지 않는다.

---

## File Structure

| 경로 | 책임 |
| --- | --- |
| `src/tracking/promotion_result.py` | 결과 enum/schema, 안전한 error 결과 생성, JSON 직렬화와 원자적 파일 기록 |
| `src/tracking/promote.py` | MLflow 후보·champion 조회, 게이트 판정, alias 이동, 정상 outcome 반환 |
| `src/tracking/registry.py` | serving calibration 준비 미달을 타입이 있는 정책 예외로 노출 |
| `src/cli.py` | legacy/구조화 모드 선택, 인자 검증, stdout·파일·exit code 어댑터 |
| `tests/test_tracking_promotion_result.py` | schema와 원자적 파일 기록 검증 |
| `tests/test_tracking_promote.py` | 세 정상 outcome과 오류 경계 검증 |
| `tests/test_tracking_registry.py` | 새 정책 예외 회귀 검증 |
| `tests/test_cli.py` | 두 CLI 모드의 출력·파일·exit code 검증 |
| `docs/specs/2026-07-25-promote-model-champion-gate.md` | legacy 계약의 후속 구조화 계약 링크 |
| `docs/specs/2026-07-13-public-batch-execution-contract.md` | `promote-model` opt-in 공개 실행 계약 |
| `.claude/docs/agent-project-reference.md` | Airflow가 소비하는 구조화 결과 경계 |

### Task 1: 타입이 있는 승격 결과와 게이트 판정

**Files:**
- Create: `src/tracking/promotion_result.py`
- Modify: `src/tracking/promote.py`
- Modify: `src/tracking/registry.py`
- Test: `tests/test_tracking_promotion_result.py`
- Test: `tests/test_tracking_promote.py`
- Test: `tests/test_tracking_registry.py`

**Interfaces:**
- Produces: `ModelPromotionResult`, `PromotionOutcome`, `PromotionReasonCode`, `PromotionExecutionError`.
- Produces: `promote.main(model_name: str, champion_alias: str) -> ModelPromotionResult`.
- Consumes: `registry.ServingCalibrationNotReadyError` from `set_model_alias`.

- [ ] **Step 1: 결과 모델의 실패 테스트를 작성한다**

`tests/test_tracking_promotion_result.py`에 다음 핵심 계약을 추가한다.

```python
def test_result_serializes_exact_v1_envelope() -> None:
    result = ModelPromotionResult(
        outcome=PromotionOutcome.REJECTED,
        model_name="ctr-model",
        champion_alias="champion",
        candidate_version="13",
        champion_version="12",
        candidate_metric=0.7812,
        champion_metric=0.7931,
        reason_code=PromotionReasonCode.METRIC_BELOW_CHAMPION,
    )

    assert result.model_dump(mode="json") == {
        "event": "model_promotion_result",
        "contract_version": "model-promotion-result-v1",
        "outcome": "rejected",
        "model_name": "ctr-model",
        "champion_alias": "champion",
        "candidate_version": "13",
        "champion_version": "12",
        "metric_name": "val_roc_auc",
        "candidate_metric": 0.7812,
        "champion_metric": 0.7931,
        "reason_code": "metric_below_champion",
    }
```

- [ ] **Step 2: 결과 모델 테스트가 실패하는지 확인한다**

Run:

```bash
uv run python -m pytest tests/test_tracking_promotion_result.py -v
```

Expected: `src.tracking.promotion_result`가 없어 collection이 실패한다.

- [ ] **Step 3: 결과 schema와 실행 오류 타입을 최소 구현한다**

`src/tracking/promotion_result.py`에 `str, Enum` 기반 outcome/reason enum과 다음
Pydantic 모델을 구현한다.

```python
class ModelPromotionResult(BaseModel):
    event: Literal["model_promotion_result"] = "model_promotion_result"
    contract_version: Literal["model-promotion-result-v1"] = (
        "model-promotion-result-v1"
    )
    outcome: PromotionOutcome
    model_name: str
    champion_alias: str
    candidate_version: str | None = None
    champion_version: str | None = None
    metric_name: Literal["val_roc_auc"] = "val_roc_auc"
    candidate_metric: float | None = None
    champion_metric: float | None = None
    reason_code: PromotionReasonCode
```

`PromotionExecutionError(RuntimeError)`는 `reason_code:
PromotionReasonCode`를 보존하되 원본 예외 문자열을 결과 필드로 만들지 않는다.
모듈 docstring은 pipeline 위치, 기능, Airflow 비책임 경계를 선언한다.

- [ ] **Step 4: 판정 본체의 기존 예외 기대를 결과 기대 테스트로 바꾼다**

`tests/test_tracking_promote.py`에서 다음을 명시적으로 검증한다.

```python
assert result.outcome is PromotionOutcome.NO_CANDIDATE
assert result.reason_code is PromotionReasonCode.REGISTRY_EMPTY

assert result.outcome is PromotionOutcome.PROMOTED
assert result.reason_code is PromotionReasonCode.METRIC_NOT_DEGRADED

assert result.outcome is PromotionOutcome.REJECTED
assert result.reason_code is PromotionReasonCode.METRIC_BELOW_CHAMPION
assert client.set_alias_calls == []
```

downsampling artifact 부재는 `CALIBRATION_ARTIFACT_MISSING`, serving guard 미준비는
`SERVING_CALIBRATION_NOT_READY`를 기대한다. 지표 결손과 artifact store 오류는
`PromotionExecutionError`의 `METRIC_MISSING`,
`ARTIFACT_LOOKUP_FAILED`를 기대한다.

- [ ] **Step 5: 바뀐 판정 테스트가 실패하는지 확인한다**

Run:

```bash
uv run python -m pytest tests/test_tracking_promote.py tests/test_tracking_registry.py -v
```

Expected: 기존 `str | None` 반환과 `GateRejectedError` 때문에 실패한다.

- [ ] **Step 6: registry 정책 예외와 `promote.main` 결과 반환을 구현한다**

`src/tracking/registry.py`에 다음 예외를 추가하고 기존 준비 가드의 `ValueError`를
교체한다.

```python
class ServingCalibrationNotReadyError(ValueError):
    """downsampling champion 승격이 serving 준비 가드에 의해 거부됨."""
```

`promote.main`은 각 정상 분기에서 `ModelPromotionResult`를 반환한다. MLflow
조회, 지표 결손, artifact 조회, alias 이동 오류는 각각 안정된
`PromotionReasonCode`를 가진 `PromotionExecutionError`로 감싼다. 단,
`ServingCalibrationNotReadyError`는 `rejected` 결과로 바꾸고 alias 호출 전
정책 미달은 어떤 외부 변경도 하지 않는다.

- [ ] **Step 7: domain 테스트를 통과시킨다**

Run:

```bash
uv run python -m pytest \
  tests/test_tracking_promotion_result.py \
  tests/test_tracking_promote.py \
  tests/test_tracking_registry.py -v
```

Expected: 모든 테스트 PASS.

- [ ] **Step 8: domain 변경을 커밋한다**

```bash
git add src/tracking/promotion_result.py src/tracking/promote.py \
  src/tracking/registry.py tests/test_tracking_promotion_result.py \
  tests/test_tracking_promote.py tests/test_tracking_registry.py
git commit -m "feat: 모델 승격 결과를 구조화"
```

### Task 2: 구조화 CLI와 원자적 결과 파일

**Files:**
- Modify: `src/tracking/promotion_result.py`
- Modify: `src/cli.py`
- Modify: `tests/test_tracking_promotion_result.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Consumes: Task 1의 `ModelPromotionResult`와 `PromotionExecutionError`.
- Produces: `write_result_file(result: ModelPromotionResult, path: Path) -> None`.
- Produces: CLI options `--result-contract`와 `--result-path`.

- [ ] **Step 1: 원자적 파일 기록 실패 테스트를 작성한다**

```python
def test_write_result_file_replaces_target_with_one_json_object(tmp_path) -> None:
    target = tmp_path / "xcom" / "return.json"
    write_result_file(_promoted_result(), target)

    assert json.loads(target.read_text())["outcome"] == "promoted"
    assert list(target.parent.glob(f".{target.name}.*.tmp")) == []
```

같은 테스트 파일에 부모 생성, 기존 파일 교체, `os.replace` 실패 전파를 추가한다.

- [ ] **Step 2: CLI 구조화 모드의 실패 테스트를 작성한다**

`tests/test_cli.py`에 `CliRunner` 또는 직접 호출 + `pytest.raises(typer.Exit)`로
다음을 검증한다.

```python
assert json.loads(result_path.read_text())["outcome"] == "rejected"
assert json.loads(captured.out.strip())["outcome"] == "rejected"
assert exit_code == 0
```

추가 cases:

- contract/path 중 하나만 지정 → exit 2, `promote.main` 미호출
- `error` → 안전한 JSON 기록 후 exit 1
- 결과 파일 쓰기 실패 → stdout `reason_code=result_write_failed`, exit 1
- legacy rejected → `[게이트 미달]`, exit 1
- legacy promoted/no candidate → 종전 문구, exit 0

기존처럼 command 함수를 직접 호출하는 테스트는 새 Typer option의
`OptionInfo` default가 함수 인자로 들어오지 않도록
`result_contract=None, result_path=None`을 명시한다.

- [ ] **Step 3: 새 CLI 테스트가 실패하는지 확인한다**

Run:

```bash
uv run python -m pytest \
  tests/test_tracking_promotion_result.py tests/test_cli.py -v
```

Expected: writer와 새 CLI 인자가 없어 실패한다.

- [ ] **Step 4: JSON writer를 구현한다**

부모 디렉터리를 만들고 같은 디렉터리의 임시 파일에 UTF-8 JSON을 한 번 쓴 뒤
flush/close하고 `os.replace`한다. 실패 시 best-effort로 임시 파일을
`unlink(missing_ok=True)`하고 예외를 전파한다.

```python
payload = result.model_dump_json()
temporary.write_text(payload, encoding="utf-8")
os.replace(temporary, path)
```

실제 구현은 충돌하지 않는 `NamedTemporaryFile(delete=False, dir=path.parent)`
경로를 사용한다.

- [ ] **Step 5: CLI 어댑터를 구현한다**

`promote_model`에 다음 타입의 옵션을 추가한다.

```python
result_contract: str | None = typer.Option(None, "--result-contract"),
result_path: Path | None = typer.Option(None, "--result-path"),
```

두 값이 모두 없으면 legacy adapter, 둘 다 있으면 v1 adapter로 분기한다. v1은
결과 파일을 먼저 완성한 뒤 같은 모델의 `model_dump_json()`을 stdout 마지막
줄에 기록한다. `PromotionExecutionError`와 예상하지 못한 예외는 각각 안전한
reason code의 `error` 결과로 바꾸고 exit 1로 종료한다.

- [ ] **Step 6: CLI와 writer 테스트를 통과시킨다**

Run:

```bash
uv run python -m pytest \
  tests/test_tracking_promotion_result.py tests/test_cli.py -v
```

Expected: 모든 테스트 PASS.

- [ ] **Step 7: CLI 변경을 커밋한다**

```bash
git add src/tracking/promotion_result.py src/cli.py \
  tests/test_tracking_promotion_result.py tests/test_cli.py
git commit -m "feat: 모델 승격 결과 파일 계약 추가"
```

### Task 3: 공개 계약과 저장소 경계 문서 갱신

**Files:**
- Modify: `docs/specs/2026-07-25-promote-model-champion-gate.md`
- Modify: `docs/specs/2026-07-13-public-batch-execution-contract.md`
- Modify: `.claude/docs/agent-project-reference.md`
- Modify: `docs/README.md`

**Interfaces:**
- Consumes: Task 2에서 실제 구현된 option, schema, exit code.
- Produces: 다른 저장소가 복사하지 않고 링크할 공개 계약.

- [ ] **Step 1: 구현과 문서의 모든 단언을 대조한다**

Run:

```bash
uv run python -m src.cli promote-model --help
rg -n "model-promotion-result-v1|result-contract|result-path" \
  src/cli.py src/tracking/promotion_result.py
```

Expected: 두 option과 contract 상수가 구현에 존재한다.

- [ ] **Step 2: 기존 champion gate spec에 후속 계약을 명시한다**

기존 “게이트 미달과 오류 모두 nonzero” 문단을 역사적 legacy 계약으로 표시하고,
구조화 호출의 세 정상 outcome exit 0과 새 spec 링크를 추가한다.

- [ ] **Step 3: 공개 batch 계약에 model promotion 절을 추가한다**

정확한 command, opt-in options, JSON 예시, exit 0/1/2, legacy compatibility,
Airflow result path 소유 경계를 기록한다. 문서의 JSON은
`ModelPromotionResult.model_json_schema()`와 필드별로 대조한다.

- [ ] **Step 4: 프로젝트 참조와 문서 인덱스를 갱신한다**

`.claude/docs/agent-project-reference.md`에는 application이 결과 schema를
소유하고 Airflow가 파일을 운반한다는 경계를 추가한다. `docs/README.md`의 중복
링크나 깨진 링크를 정리한다.

- [ ] **Step 5: 문서 검증을 실행한다**

Run:

```bash
rg -n "model-promotion-result-v1|result-contract|result-path" \
  docs/specs/2026-07-25-promote-model-champion-gate.md \
  docs/specs/2026-07-13-public-batch-execution-contract.md \
  .claude/docs/agent-project-reference.md
git diff --check
```

Expected: 모든 정본에 계약이 있고 whitespace 오류가 없다.

- [ ] **Step 6: 문서를 커밋한다**

```bash
git add docs/specs/2026-07-25-promote-model-champion-gate.md \
  docs/specs/2026-07-13-public-batch-execution-contract.md \
  .claude/docs/agent-project-reference.md docs/README.md
git commit -m "docs: 모델 승격 결과 계약 반영"
```

### Task 4: 전체 회귀 검증과 실행 계획 아카이브

**Files:**
- Move: `docs/plans/2026-07-29-model-promotion-structured-outcome.md` → `docs/archive/plans/2026-07-29-model-promotion-structured-outcome.md`
- Modify: `docs/README.md`

**Interfaces:**
- Consumes: Tasks 1-3 전체 변경.
- Produces: Airflow가 통합할 수 있는 검증된 application revision.

- [ ] **Step 1: 관련 테스트와 lint를 실행한다**

Run:

```bash
uv run python -m pytest \
  tests/test_tracking_promotion_result.py \
  tests/test_tracking_promote.py \
  tests/test_tracking_registry.py \
  tests/test_cli.py -v
uv run --no-sync ruff check src/tracking/promotion_result.py \
  src/tracking/promote.py src/tracking/registry.py src/cli.py \
  tests/test_tracking_promotion_result.py tests/test_tracking_promote.py \
  tests/test_tracking_registry.py tests/test_cli.py
```

Expected: 0 failures, 0 lint errors.

- [ ] **Step 2: 전체 CI 동등 검증을 실행한다**

Run:

```bash
uv run python -m pytest
uv run --no-sync ruff check autoresearch tests tools
git diff --check
```

Expected: pytest와 ruff가 exit 0이고 whitespace 오류가 없다.

- [ ] **Step 3: 시크릿과 결과 schema drift를 점검한다**

Run:

```bash
rg -n "hooks\\.slack\\.com|xox[baprs]-|BEGIN .*PRIVATE KEY" \
  src tests docs .claude || true
git status --short
git diff HEAD~3 -- src tests docs .claude
```

Expected: credential 실값이 없고 diff가 #411 범위에 한정된다.

- [ ] **Step 4: 완료된 plan을 archive로 옮긴다**

```bash
mkdir -p docs/archive/plans
git mv docs/plans/2026-07-29-model-promotion-structured-outcome.md \
  docs/archive/plans/2026-07-29-model-promotion-structured-outcome.md
```

`docs/README.md`에서 진행 중 plan 링크가 있으면 archive 링크로 바꾼다.

- [ ] **Step 5: 최종 문서 상태를 커밋한다**

```bash
git add docs/archive/plans/2026-07-29-model-promotion-structured-outcome.md \
  docs/README.md
git commit -m "docs: 모델 승격 구현 계획 보관"
```

## Deployment Handoff

이 저장소의 로컬 구현이 끝나도 최신 code archive를 게시하지 않는다. Airflow
브랜치가 `--result-contract`와 `--result-path`를 지원하는 상태인지 확인한 뒤,
운영 변경 승인을 받아 application archive → Airflow callback 순서로 배포한다.
