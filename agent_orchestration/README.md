# agent_orchestration

`FastAPI + Codex CLI + PostgreSQL` 기반 기본 스켈레톤입니다.
1단계 목표는 `/chat` 호출 결과를 팀 공용 ChatGPT 계정으로 OAuth 로그인된 Codex
CLI에서 받은 텍스트와 함께 PostgreSQL에 저장하는 것입니다. 기본 모드는 OpenAI
API 키나 API 크레딧을 사용하지 않습니다.

## 로컬 실행(1단계)

1. 서버를 실행할 **운영 계정**에서 Codex CLI OAuth 로그인을 한 번 완료합니다.
   앱이 OAuth 토큰을 받거나 저장하지 않으며, 팀 공용 계정을 사용할 때에는 모든
   요청이 그 계정의 구독 한도를 공유합니다.
   ```bash
   codex login
   codex login status
   ```
2. 저장소 루트에서 가상환경을 준비한 뒤 의존성을 반영합니다.
   ```bash
   uv sync --extra dev
   ```
3. 예시 파일을 로컬 환경 파일로 복사합니다.
   ```bash
   cp .env.example .env
   ```
4. `.env`에서 아래 값을 확인합니다.
   - `LLM_BACKEND=codex_cli`
   - `ORCH_DATABASE_URL` (또는 `DATABASE_URL`)
   - 필요 시 `CODEX_MODEL` (비우면 Codex CLI 기본 모델)

   OAuth 토큰이나 `OPENAI_API_KEY`는 이 기본 모드에 넣지 않습니다.
5. PostgreSQL 컨테이너 기동 및 healthcheck 완료 대기
   ```bash
   docker compose -f agent_orchestration/docker-compose.yml up -d --wait
   ```
6. 앱 실행
   ```bash
   uv run uvicorn agent_orchestration.app.main:app --env-file .env --host 0.0.0.0 --port 8000
   ```
7. 요청 예시
   ```bash
   curl -X POST http://127.0.0.1:8000/chat \
     -H "Content-Type: application/json" \
     -d '{"prompt":"CTR 개선 방안을 3줄로 요약해줘"}'
   ```
8. 저장 확인
   ```bash
   docker exec -it $(docker ps -q --filter "ancestor=postgres:15") \
     psql -U orch_user -d orch_orchestration \
     -c "SELECT id, model, created_at FROM chat_interactions ORDER BY id DESC LIMIT 3;"
   ```

## 현재 범위(스케일 업 전)

- 단일 채팅 저장만 구현
- 인증/권한/세션은 제외 (2단계 또는 추후 스펙)
- 배포 시에도 서버 운영 계정의 Codex CLI 로그인 정보가 유지되도록 해당 계정의
  Codex 홈 디렉터리를 시크릿 볼륨으로 제공해야 한다.
- 사용자별 Google/Codex 계정 연결과 사용자별 사용량 분리는 후속 범위다.

## OpenAI API 전환(후속)

API 결제 방식으로 전환할 때만 `LLM_BACKEND=openai`와 `OPENAI_API_KEY`를 설정한다.
그 외 `/chat`과 PostgreSQL 저장 계약은 유지한다.
