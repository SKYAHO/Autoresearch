# Codex CLI 공용 계정 백엔드 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 팀 공용 ChatGPT OAuth로 로그인된 Codex CLI의 응답을 FastAPI `/chat`에서 받아 PostgreSQL에 저장한다.

**Architecture:** LLM 호출을 `agent_orchestration.app.llm` 모듈로 분리한다. 기본 백엔드인 `codex_cli`는 비어 있는 임시 디렉터리에서 읽기 전용·일회성 Codex CLI 프로세스를 실행하고, `openai` 백엔드는 향후 전환용으로 기존 Responses API 호출을 유지한다.

**Tech Stack:** Python 3.11/3.12, FastAPI, `asyncio` subprocess, Codex CLI OAuth, psycopg 3, pytest.

## 전역 제약

- 기본값은 `LLM_BACKEND=codex_cli`이며, 공용 운영 계정이 서비스 시작 전에 `codex login`을 완료한다.
- Codex CLI 호출은 `--sandbox read-only --ephemeral --skip-git-repo-check`와 임시 작업 디렉터리를 사용한다.
- OAuth 자격 증명·프롬프트·전체 stderr는 로그, DB, 환경 변수에 기록하지 않는다.
- Codex CLI 결과의 `token_count`는 `None`으로 저장한다.
- `LLM_BACKEND=openai`일 때만 `OPENAI_API_KEY`를 요구한다.

---

### Task 1: 설정과 LLM 호출 경계

**Files:**
- Create: `agent_orchestration/app/llm.py`
- Modify: `agent_orchestration/app/config.py`
- Test: `tests/test_agent_orchestration.py`

**Interfaces:**
- Produces `async def generate_response(settings: ServiceSettings, prompt: str) -> LLMResult`.
- `LLMResult`는 `text: str`, `model: str`, `token_count: int | None`을 제공한다.
- `LLMBackendError`는 안전한 백엔드 실패를 API 계층에 전달한다.

- [ ] **Step 1: Codex 설정의 실패 테스트를 작성한다.**

```python
def test_load_settings_allows_codex_without_openai_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_BACKEND", "codex_cli")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("ORCH_DATABASE_URL", "postgresql://orch:pw@localhost:5432/orch")

    settings = load_settings()

    assert settings.llm_backend == "codex_cli"
    assert settings.openai_api_key is None
```

- [ ] **Step 2: 테스트가 현재 `OPENAI_API_KEY` 필수 검증에서 실패하는지 실행한다.**

Run: `uv run python -m pytest tests/test_agent_orchestration.py::test_load_settings_allows_codex_without_openai_key -q`

Expected: `ValueError` 발생으로 FAIL.

- [ ] **Step 3: `ServiceSettings`에 `llm_backend`, `codex_cli_path`, `codex_model`, `codex_timeout_sec`을 추가한다.**

```python
llm_backend = os.getenv("LLM_BACKEND", "codex_cli").strip().lower()
if llm_backend not in {"codex_cli", "openai"}:
    raise ValueError("LLM_BACKEND must be one of: codex_cli, openai.")
openai_api_key = (
    _require_env("OPENAI_API_KEY", os.getenv("OPENAI_API_KEY"))
    if llm_backend == "openai"
    else None
)
```

- [ ] **Step 4: Codex CLI 실행의 실패 테스트를 작성한다.**

```python
def test_generate_response_uses_read_only_ephemeral_codex_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    # create_subprocess_exec fake는 -o 출력 파일에 "local answer"를 쓰고
    # stdin으로 받은 프롬프트와 command 인자를 보관한다.
    result = asyncio.run(generate_response(settings, "질문"))
    assert result == LLMResult("local answer", "codex-cli", None)
    assert "--sandbox" in command and "read-only" in command
    assert stdin_payload == b"질문"
```

- [ ] **Step 5: `llm.py`에 shell 없이 Codex와 OpenAI 백엔드를 구현한다.**

```python
process = await asyncio.create_subprocess_exec(
    *command,
    "-",
    stdin=asyncio.subprocess.PIPE,
    stdout=asyncio.subprocess.DEVNULL,
    stderr=asyncio.subprocess.PIPE,
)
await asyncio.wait_for(process.communicate(prompt.encode()), timeout=settings.codex_timeout_sec)
```

Codex는 `TemporaryDirectory()`의 `-C` 경로와 `--output-last-message` 파일만 사용한다. 종료 코드가 0이 아니거나 결과 텍스트가 비면 `LLMBackendError`를 던진다. 시간 초과 시 프로세스를 종료하고 회수한다.

- [ ] **Step 6: Task 1 테스트를 통과시킨다.**

Run: `uv run python -m pytest tests/test_agent_orchestration.py -q`

Expected: PASS.

### Task 2: API와 문서의 백엔드 전환

**Files:**
- Modify: `agent_orchestration/app/main.py`
- Modify: `.env.example`
- Modify: `agent_orchestration/README.md`
- Modify: `docs/specs/2026-07-30-agent-orchestration-chat-postgres-skeleton.md`
- Modify: `tests/test_agent_orchestration.py`

**Interfaces:**
- `main.py`은 `generate_response()` 결과를 `save_interaction()`에 전달한다.
- `LLMBackendError`는 `/chat`에서 HTTP 502로 변환한다.

- [ ] **Step 1: Codex 백엔드 오류의 API 실패 테스트를 작성한다.**

```python
async def failing_generate_response(*_args: object, **_kwargs: object) -> LLMResult:
    raise LLMBackendError("Codex CLI failed")

monkeypatch.setattr(main_module, "generate_response", failing_generate_response)
assert client.post("/chat", json={"prompt": "테스트"}).status_code == 502
```

- [ ] **Step 2: 새 테스트가 기존 `AsyncOpenAI` 직접 호출 구조에서 실패하는지 실행한다.**

Run: `uv run python -m pytest tests/test_agent_orchestration.py::test_main_chat_returns_bad_gateway_when_codex_cli_fails -q`

Expected: import 또는 monkeypatch 대상 부재로 FAIL.

- [ ] **Step 3: `main.py`에서 직접 OpenAI 호출을 제거하고 `generate_response()`를 호출한다.**

```python
try:
    generated = await generate_response(runtime_settings, request.prompt)
except LLMBackendError as error:
    raise HTTPException(status_code=502, detail="Failed to call LLM backend.") from error
```

저장 시 `generated.text`, `generated.model`, `generated.token_count`를 사용한다.

- [ ] **Step 4: `.env.example`과 README에 공용 계정 운영 절차를 적는다.**

```dotenv
LLM_BACKEND=codex_cli
CODEX_CLI_PATH=codex
CODEX_MODEL=
CODEX_TIMEOUT_SEC=120
```

README에는 서비스 운영 계정으로 `codex login`을 먼저 완료하고, Codex 홈 디렉터리를 시크릿 볼륨으로 보관하며, `.env`에 OAuth 토큰을 넣지 않는 절차를 명시한다.

- [ ] **Step 5: Task 2 테스트와 린트를 통과시킨다.**

Run: `uv run python -m pytest tests/test_agent_orchestration.py -q`

Run: `uv run --no-sync ruff check agent_orchestration tests`

Expected: PASS.

### Task 3: 실제 공용 Codex OAuth와 PostgreSQL 검증

**Files:**
- Modify: `docs/plans/2026-07-30-agent-orchestration-chat-postgres-skeleton.md`

**Interfaces:**
- `POST /chat`은 Codex CLI 최종 텍스트를 201로 반환하고 동일한 값이 `chat_interactions`에 저장된다.

- [ ] **Step 1: 실행 전 Codex OAuth 상태를 확인한다.**

Run: `codex login status`

Expected: `Logged in using ChatGPT`.

- [ ] **Step 2: PostgreSQL이 healthy 상태인지 확인한다.**

Run: `docker compose -f agent_orchestration/docker-compose.yml ps`

Expected: `postgres`가 `healthy`.

- [ ] **Step 3: `LLM_BACKEND=codex_cli`로 Uvicorn을 기동하고 짧은 `/chat` 요청을 1회 보낸다.**

```bash
LLM_BACKEND=codex_cli \
ORCH_DATABASE_URL=postgresql://orch_user:orch_password@localhost:5432/orch_orchestration \
uv run uvicorn agent_orchestration.app.main:app --host 127.0.0.1 --port 8000
```

Expected: `201` 응답, `model`은 `codex-cli` 또는 `CODEX_MODEL` 값, `token_count`는 `null`.

- [ ] **Step 4: DB에서 반환 응답과 동일한 최신 행을 조회한다.**

Run: `docker compose -f agent_orchestration/docker-compose.yml exec -T postgres psql -U orch_user -d orch_orchestration -c "SELECT model, token_count, response FROM chat_interactions ORDER BY id DESC LIMIT 1;"`

Expected: API 응답과 같은 `response`, `token_count`는 null.

- [ ] **Step 5: 전체 검증을 실행한다.**

Run: `uv run python -m pytest -q`

Run: `uv run --no-sync ruff check autoresearch tests tools agent_orchestration`

Run: `git diff --check`

Expected: 모든 명령이 성공한다.
