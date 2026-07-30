# agent_orchestration

`FastAPI + Codex CLI + PostgreSQL` 기반 기본 스켈레톤입니다.
1단계 목표는 `/chat` 호출 결과를 팀 공용 ChatGPT 계정으로 OAuth 로그인된 Codex
CLI에서 받은 텍스트와 함께 PostgreSQL에 저장하는 것입니다. 기본 모드는 OpenAI
API 키나 API 크레딧을 사용하지 않습니다.

## 로컬 실행(1단계)

1. Codex 전용 홈 디렉터리를 만들고, 서버를 실행할 **운영 계정**에서 그 경로로
   Codex CLI OAuth 로그인을 완료합니다. 앱은 OAuth 토큰을 받거나 저장하지 않으며,
   팀 공용 계정을 사용할 때에는 모든 요청이 그 계정의 구독 한도를 공유합니다.
   ```bash
   export CODEX_HOME="$HOME/.agent-orchestration-codex"
   mkdir -p "$CODEX_HOME"
   CODEX_HOME="$CODEX_HOME" codex login
   CODEX_HOME="$CODEX_HOME" codex login status
   ```
   이 개발 환경에서 확인한 CLI 버전은 `codex-cli 0.146.0`입니다. 배포 이미지는
   이 명령 인자 계약을 검증한 Codex CLI 버전을 고정해야 하며, 업그레이드 시에는
   `--sandbox`, `--ephemeral`, `--skip-git-repo-check`, `-C`, `-o` 호출을
   별도 smoke test로 다시 검증합니다.
2. 저장소 루트에서 가상환경을 준비한 뒤 의존성을 반영합니다.
   ```bash
   uv sync
   ```
3. 예시 파일을 로컬 환경 파일로 복사합니다.
   ```bash
   cp .env.example .env
   ```
4. `.env`에서 아래 값을 확인합니다.
   - `LLM_BACKEND=codex_cli`
   - `ORCH_DATABASE_URL` (또는 `DATABASE_URL`)
   - `ORCH_API_TOKEN` (32자 이상의 임의 공유 토큰)
   - `CODEX_HOME` (위에서 로그인한 전용 절대 경로)
   - 필요 시 `CODEX_MODEL` (비우면 Codex CLI 기본 모델)

   OAuth 토큰이나 `OPENAI_API_KEY`는 이 기본 모드에 넣지 않습니다. 토큰은 예를
   들어 `openssl rand -hex 32`로 생성합니다.
5. PostgreSQL 컨테이너 기동 및 healthcheck 완료 대기
   ```bash
   docker compose -f agent_orchestration/docker-compose.yml up -d --wait
   ```
6. 앱 실행
   ```bash
   uv run uvicorn agent_orchestration.app.main:app --env-file .env --host 127.0.0.1 --port 8000
   ```
7. 요청 예시
   ```bash
   curl -X POST http://127.0.0.1:8000/chat \
     -H "Content-Type: application/json" \
     -H "X-Orch-Token: <ORCH_API_TOKEN>" \
     -d '{"prompt":"CTR 개선 방안을 3줄로 요약해줘"}'
   ```
8. 저장 확인
   ```bash
   docker compose -f agent_orchestration/docker-compose.yml exec postgres \
     psql -U orch_user -d orch_orchestration \
     -c "SELECT id, model, created_at FROM chat_interactions ORDER BY id DESC LIMIT 3;"
   ```

## 현재 범위(스케일 업 전)

- 단일 채팅 저장만 구현
- 사용자별 인증/권한/세션은 제외 (2단계 또는 추후 스펙). 대신 `/chat`은 공유
  `X-Orch-Token` 헤더를 요구한다.
- 배포 시에도 서버 운영 계정의 Codex CLI 로그인 정보가 유지되도록 해당 계정의
  전용 Codex 홈 디렉터리를 시크릿 볼륨으로 제공해야 한다. Codex 하위 프로세스는
  이 홈과 `PATH`만 받으며 부모 프로세스의 DB·API 토큰 환경 변수를 상속하지 않는다.
  그러나 `read-only` sandbox는 파일 읽기를 허용하므로, 이 조치만으로
  `$CODEX_HOME/auth.json`을 프롬프트 기반 파일 읽기에서 보호하지는 못한다.
  **현재 스켈레톤은 공용 OAuth 자격 증명을 가진 채로 배포할 수 없다.** 배포 전에는
  Codex 도구 프로세스가 자격 증명 파일을 읽을 수 없도록 보장하는 전용 실행 격리와
  유출 시 `codex logout`·재로그인 절차를 별도 배포 스펙으로 확정해야 한다.
  이 제한은 로컬 공용 계정 검증에도 동일하므로, 신뢰하는 개인 개발 환경에서만
  비민감 프롬프트로 실행한다. 응답·stderr 마스킹은 보조 진단 수단일 뿐 OAuth
  자격 증명 보호 경계가 아니다.
- 사용자별 Google/Codex 계정 연결과 사용자별 사용량 분리는 후속 범위다.
- **신뢰 네트워크 밖에 노출하지 않는다.** 로컬 실행은 반드시 `127.0.0.1`에만
  바인딩한다. 배포 시에는 저권한 전용 컨테이너/계정, 비공개 Service와 네트워크
  정책을 추가로 적용한다. 공유 토큰이 유출되면 Codex 프롬프트가 서버 파일을 읽도록
  유도될 위험을 완전히 제거하지 못한다.
- `prompt`와 `response`는 PostgreSQL에 평문으로 저장한다. 1단계에서는 로컬 검증용
  비민감 데이터만 사용하며, 보존 기간·마스킹 정책은 배포 전 별도 스펙으로 정한다.
- 저장이 실패하면 LLM 응답도 반환하지 않고 `500`을 반환한다. 저장이 목표인 API이므로
  클라이언트·프록시·로드밸런서는 동일 프롬프트를 자동 재시도하지 않는다. 현재 API는
  멱등 키를 제공하지 않으므로, 재시도하면 공용 계정 사용량이 다시 소모되고 저장이
  성공한 요청은 중복 레코드가 될 수 있다.
- `CODEX_MODEL`을 비우면 저장되는 모델 값은 `codex-cli`이다. Codex CLI 기본 모델의
  실제 식별자는 이 스켈레톤에서 조회하지 않는다.
- 현재 스키마 보장은 최초 `CREATE TABLE IF NOT EXISTS`만 수행한다. 컬럼 변경이 필요할
  때는 별도 마이그레이션 절차를 도입한다. 시작 시 같은 테이블명에 대한 PostgreSQL
  advisory lock으로 다중 레플리카의 최초 생성 경쟁을 직렬화하지만, 이는 스키마
  마이그레이션 도구가 아니다. 이름만 같은 다른 스키마의 테이블이 있으면 기동은
  성공하지만 첫 저장 시 SQL 오류로 `500`을 반환한다. 이후 호환 가능한 컬럼 추가는
  스키마를 먼저 확장한 뒤 해당 컬럼을 선택적으로 쓰는 코드를 배포하며, 롤백은 코드만
  되돌리고 확장 컬럼은 유지하는 방식으로 관리한다.
- `/healthcheck`는 설정 검증과 초기 스키마 준비를 통과해 프로세스가 기동했음을
  보장한다. Codex OAuth 세션, Codex 바이너리, 기동 후 PostgreSQL 연결 상태는 검사하지
  않으므로 Kubernetes readiness probe로 사용하기 전에 별도 상태 확인 계약을 정한다.
- 현재는 요청마다 PostgreSQL 연결과 Codex 하위 프로세스를 하나씩 만들며 동시 요청
  상한이 없다. 배포 전에는 `psycopg_pool.ConnectionPool`과 Codex 실행
  `asyncio.Semaphore`를 도입하고, 한도를 넘는 요청의 `429` 또는 `503` 계약을 정한다.
- 응답의 `latency_ms`는 LLM 호출 시간만 나타내며 PostgreSQL 저장 시간은 포함하지
  않는다.
- Codex 하위 프로세스의 `HOME`, `TMPDIR`, XDG cache/state 경로는 요청별 임시 디렉터리로
  한정한다. 배포 runner는 이 쓰기 경로를 별도 emptyDir로 제공하고, 프록시·CA 등
  네트워크 연결에 필요한 변수는 부모 환경을 상속하지 않고 명시적으로 검증·주입해야
  한다. 이 조건 역시 OAuth runner 배포 전 smoke test 대상이다.

## OpenAI API 전환(후속)

API 결제 방식으로 전환할 때만 `LLM_BACKEND=openai`와 `OPENAI_API_KEY`를 설정한다.
그 외 `/chat`과 PostgreSQL 저장 계약은 유지한다.

## Codex OAuth 배포 설계(후속)

공용 Codex OAuth를 계속 사용할 경우 API 서버에서 Codex CLI를 직접 실행하지 않는다.
구체적인 보안 경계·모델 전환·배포 전 검증 기준은
[`docs/specs/2026-07-30-codex-oauth-runner-isolation.md`](../docs/specs/2026-07-30-codex-oauth-runner-isolation.md)를
따른다. 이 설계가 구현·검증되기 전에는 Codex OAuth 모드로 외부 배포하지 않는다.
