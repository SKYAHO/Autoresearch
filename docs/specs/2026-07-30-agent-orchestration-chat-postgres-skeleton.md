# Agent Orchestration 채팅 저장 스켈레톤 계약 (Spec)

## 배경

1단계 목표는 FastAPI로 `/chat` API를 제공하고, 사용자 프롬프트를 OpenAI
(`gpt-5.3-codex-spark`)에 전달한 뒤 응답을 PostgreSQL에 저장하는
최소 동작 경로를 마련하는 것이다.

## 책임 (범위)

- `/chat` 입력(Prompt)을 단건으로 받아 OpenAI Responses API를 호출한다.
- 응답 본문, 사용 모델명, 지연시간, token 사용량을 함께 `chat_interactions`
  테이블에 저장한다.
- 저장 실패 또는 LLM 호출 실패를 HTTP 에러 코드로 구분해 반환한다.
- 운영 환경에 바로 배포하기보다는 로컬 검증 가능한 스켈레톤 상태로 시작한다.

## 로컬 Codex CLI 모드

로컬 검증 시에는 ChatGPT OAuth로 로그인된 Codex CLI를 LLM 제공자로 선택할 수
있다. 이 모드는 사용자의 ChatGPT 구독 사용량을 사용하며 OpenAI API 키나 API
크레딧을 요구하지 않는다.

- `LLM_BACKEND=codex_cli`일 때만 Codex CLI를 호출한다.
- Codex CLI는 `--sandbox read-only`, `--ephemeral`, `--skip-git-repo-check`로
  실행한다. 따라서 요청 프롬프트는 저장소를 수정하지 않고, 세션 자격 증명이나
  대화 기록도 앱이 저장하지 않는다.
- CLI가 반환한 최종 텍스트만 PostgreSQL에 저장한다. Codex CLI는 이 경로에서
  토큰 사용량을 제공하지 않으므로 `token_count`는 `NULL`로 저장한다.
- `CODEX_MODEL`을 설정하면 해당 모델을 Codex CLI에 전달하고, 비워 두면 이미
  로그인된 Codex CLI의 기본 모델을 사용한다.
- CLI 실행 실패·시간 초과는 `502`로 변환한다. 로그에는 프롬프트나 OAuth 토큰을
  기록하지 않는다.

배포 환경은 `LLM_BACKEND=openai`(기본값)를 사용해 OpenAI Responses API로
전환한다. 이 경우에만 `OPENAI_API_KEY`가 필수다.

## 설정 계약

- 필수:
  - `ORCH_DATABASE_URL` 또는 기존 공용 `DATABASE_URL`
- `LLM_BACKEND=openai`일 때 필수:
  - `OPENAI_API_KEY`
- 기본값:
  - `LLM_BACKEND=openai`
  - `OPENAI_MODEL=gpt-5.3-codex-spark`
  - `OPENAI_MAX_TOKENS=1024`
  - `OPENAI_TIMEOUT_SEC=60`
  - `ORCH_INTERACTIONS_TABLE=chat_interactions`
- `LLM_BACKEND=codex_cli`일 때 선택값:
  - `CODEX_CLI_PATH=codex`
  - `CODEX_MODEL=` (비우면 Codex CLI 기본 모델 사용)
  - `CODEX_TIMEOUT_SEC=120`

## API 계약

### `GET /healthcheck`
- 성공: `200 {"status":"ok","service":"agent-orchestration"}`
- 초기화 미완료: `503` (service unavailable)

### `POST /chat`

요청:
```json
{"prompt":"string (1~8192)"}
```

성공 응답(201):
```json
{
  "id": 1,
  "prompt": "...",
  "response": "...",
  "model": "gpt-5.3-codex-spark",
  "latency_ms": 123,
  "token_count": 45,
  "created_at": "2026-07-30T00:00:00Z"
}
```

에러:
- OpenAI 호출 실패: `502`
- LLM 응답 비정상(빈 text output): `502`
- DB 저장 실패: `500`

## DB 계약

기본 저장 테이블은 `chat_interactions`로 아래 컬럼을 최소 보유한다.
- `id BIGSERIAL PRIMARY KEY`
- `prompt TEXT NOT NULL`
- `response TEXT NOT NULL`
- `model TEXT NOT NULL`
- `latency_ms INTEGER NOT NULL`
- `token_count INTEGER NULL`
- `created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()`
