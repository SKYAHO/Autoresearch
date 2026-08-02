# agent_orchestration

`FastAPI + Codex CLI + PostgreSQL` 기반 Agent Orchestration API입니다. 기존 `/chat`은
Codex 응답을 PostgreSQL에 저장하며, 실험 워크벤치 v0은 Agent와 Streamlit이 실험 상태,
Event, Log, metadata를 조회·기록하는 별도 API를 제공합니다. 로컬 개발의 기본 백엔드는
`codex_cli`이며, GKE API는 비공개 `codex_runner` Service를 호출합니다. 기본 모드는
OpenAI API 키나 API 크레딧을 사용하지 않습니다.

## GKE 이미지 경계

- `deploy/agent_orchestration/api.Dockerfile`은 FastAPI·PostgreSQL 런타임과 API
  소스만 포함합니다. Codex CLI, Node 런타임, OAuth 상태는 포함하지 않습니다.
- `deploy/agent_orchestration/runner.Dockerfile`만 `@openai/codex@0.146.0`과 Runner
  소스를 포함합니다. 기본 `CODEX_HOME`은 `/var/lib/codex`, 임시 경로는 `/tmp`이며,
  Kubernetes는 각각 Runner 전용 PVC와 scratch `emptyDir`를 마운트합니다.
- 두 이미지는 UID/GID `10001`의 `appuser`로 실행합니다. OAuth 파일·`.env`·완성
  데이터베이스 URL은 빌드 문맥과 이미지에 넣지 않습니다.

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
6. 실험 워크벤치 테이블 migration 적용
   ```bash
   uv run alembic -c agent_orchestration/alembic.ini upgrade head
   ```
   `/chat`의 `chat_interactions` 초기 테이블은 기존 psycopg 경로가 기동 시 보장하며,
   `experiments`, `experiment_events`, `experiment_logs`, `experiment_metadata`는 Alembic이
   관리합니다.
7. 앱 실행
   ```bash
   uv run uvicorn agent_orchestration.app.main:app --env-file .env --host 127.0.0.1 --port 8000
   ```
8. 요청 예시
   ```bash
   curl -X POST http://127.0.0.1:8000/chat \
     -H "Content-Type: application/json" \
     -H "X-Orch-Token: <ORCH_API_TOKEN>" \
     -d '{"prompt":"CTR 개선 방안을 3줄로 요약해줘"}'
   ```
9. 저장 확인
   ```bash
   docker compose -f agent_orchestration/docker-compose.yml exec postgres \
     psql -U orch_user -d orch_orchestration \
     -c "SELECT id, model, created_at FROM chat_interactions ORDER BY id DESC LIMIT 3;"
   ```

## 실험 워크벤치 v0

- 모든 실험 endpoint는 기존 `/chat`과 동일한 `X-Orch-Token`을 요구합니다.
- `POST /experiments`, `GET /experiments`, `GET /experiments/{id}`로 실험을 생성·조회합니다.
- `PATCH /experiments/{id}/status`와 `POST /experiments/{id}/events`는 승인된 상태 전이만
  허용하며 위반은 `409`를 반환합니다. `PROMOTED`는 일반 경로에서 허용하지 않습니다.
- `POST/GET /experiments/{id}/logs`와 `GET /experiments/{id}/events`는 idempotency key와
  cursor polling을 제공하며, Streamlit은 1초마다 `after_id`를 사용해 새 row만 조회합니다.
- `POST /experiments/{id}/promote`는 `PASSED` 실험에 대해 운영자가 필수 `reason`과
  idempotency key를 남기는 전용 수동 승격 경로입니다.

상세 계약과 구현 이력은
[`docs/archive/specs/2026-08-01-agent-orchestration-experiment-workbench-v0.md`](../docs/archive/specs/2026-08-01-agent-orchestration-experiment-workbench-v0.md)를
참조합니다.

## 현재 범위(스케일 업 전)

- `/chat` 저장과 실험 워크벤치 v0 API만 구현. GitHub webhook 자동 승격, 사람·Agent 인증
  분리, 실제 실험 실행기는 후속 범위
- 사용자별 인증/권한/세션은 제외 (2단계 또는 추후 스펙). 대신 `/chat`은 공유
  `X-Orch-Token` 헤더를 요구한다.
- **배포 시 Codex OAuth 홈은 비공개 Runner에만 전용 PVC로 마운트한다.** API Pod와
  API 이미지는 OAuth 파일을 갖지 않는다. Codex 하위 프로세스는 `CODEX_HOME`을 읽을
  수 있고 `read-only` sandbox도 파일 읽기를 허용하므로, Runner 자체도 신뢰 네트워크·
  최소 권한·비민감 프롬프트 범위 안에서만 사용한다. 유출 의심 시 `codex logout`·
  재로그인을 수행한다. Codex `stderr`는 수집하거나 애플리케이션 로그에 남기지 않으며,
  응답 문자열의 마스킹은 OAuth 자격 증명 보호 경계가 아니다.
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
- API `/healthcheck`는 설정 검증과 초기 스키마 준비를 통과해 프로세스가 기동했음을
  보장한다. Runner `/healthcheck`는 첫 생성 요청 전에 Runner 전용 설정과 비대기 실행
  용량 토큰을 준비해 Kubernetes readiness/liveness probe에 사용한다. 두 endpoint 모두 Codex OAuth
  세션·실제 Codex 호출·기동 후 PostgreSQL 연결 상태는 검사하지 않는다.
- 현재는 요청마다 PostgreSQL 연결을 만들며 connection pool은 후속 범위다. GKE Runner는
  `RUNNER_MAX_CONCURRENCY`(기본 1)로 Codex 실행을 제한하고, 사용 중인 슬롯이 있으면
  대기열을 만들지 않고 `503`을 반환한다. API는 이 `503`을 그대로 호출자에게 전달한다.
- 응답의 `latency_ms`는 LLM 호출 시간만 나타내며 PostgreSQL 저장 시간은 포함하지
  않는다.
- Swagger(`/docs`)는 `X-Orch-Token`을 필수 헤더로 표시하고, 인증 실패(`401`), LLM
  호출 실패(`502`), Runner 과부하(`503`), 저장 실패(`500`) 응답을 명시한다. 헤더가 실제로 누락되거나
  틀린 경우에도 런타임 응답은 `401`이다.
- Codex 하위 프로세스의 `HOME`, `TMPDIR`, XDG cache/state 경로는 요청별 임시 디렉터리로
  한정한다. 배포 runner는 이 쓰기 경로를 별도 emptyDir로 제공하고, 프록시·CA 등
  네트워크 연결에 필요한 변수는 부모 환경을 상속하지 않고 명시적으로 검증·주입해야
  한다. 이 조건 역시 OAuth runner 배포 전 smoke test 대상이다.

## OpenAI API 전환(후속)

API 결제 방식으로 전환할 때만 `LLM_BACKEND=openai`와 `OPENAI_API_KEY`를 설정한다.
그 외 `/chat`과 PostgreSQL 저장 계약은 유지한다.

## Codex 백엔드 구분

- 로컬 개발: `LLM_BACKEND=codex_cli`(기본값). API 프로세스가 요청별 격리된
  Codex CLI를 직접 실행합니다.
- GKE API: `LLM_BACKEND=codex_runner`, 절대 URL `CODEX_RUNNER_URL`, 별도 내부 토큰
  `ORCH_RUNNER_TOKEN`을 설정합니다. API는 `X-Runner-Token`으로 비공개 Runner의
  `POST /v1/generate`만 호출합니다. `ORCH_API_TOKEN`과 `ORCH_RUNNER_TOKEN`은 각각
  32자 이상이어야 하며 서로 다른 값이어야 합니다. 같으면 API는 기동을 거부합니다.
  Runner는 기동 시 `CODEX_TIMEOUT_SEC + 5 < CODEX_RUNNER_TIMEOUT_SEC`를 검증합니다.
  GKE에서는 Codex 제한 110초, API Runner HTTP 제한 120초를 하나의 ConfigMap key로
  양쪽 deployment에 주입해 Codex 하위 프로세스 종료에 5초를 남깁니다.
- 로컬 `codex_cli`도 `CODEX_TIMEOUT_SEC`을 별도로 설정하지 않으면 110초를 사용합니다.
  `CODEX_RUNNER_TIMEOUT_SEC`은 GKE API의 Runner HTTP 호출과 Runner 기동 검증에 모두
  적용됩니다. Runner에서는 이 값을 생략하면 fail-close합니다.
- Runner는 API 공유 토큰·PostgreSQL 설정을 읽지 않으며, `ORCH_RUNNER_TOKEN`만 API와
  공유합니다. OAuth 자격 증명 값은
  문서·환경 예시·애플리케이션 로그에 기록하지 않습니다.

## Codex OAuth 배포 설계

공용 Codex OAuth를 계속 사용할 경우 API 서버에서 Codex CLI를 직접 실행하지 않는다.
구체적인 보안 경계·모델 전환·배포 전 검증 기준은
[`docs/specs/2026-07-30-codex-oauth-runner-isolation.md`](../docs/specs/2026-07-30-codex-oauth-runner-isolation.md)를
따른다. API와 Runner는 별도 이미지·KSA/GSA·Service·NetworkPolicy로 배포하며,
이미지 digest와 Terraform 입력이 확정되기 전에는 ArgoCD sync를 수행하지 않는다.
Runner OAuth bootstrap init container는 **API 이미지**를 실행하지만 Runner 전용
KSA/GSA와 PVC를 사용해 OAuth 시크릿 하나만 읽는다. 이는 API 런타임 Pod에 OAuth
파일·PVC·Secret Manager 권한을 주지 않는 분리 경계를 유지하기 위한 선택이다.
이 init container는 `python -m agent_orchestration.bootstrap_secrets runner-codex-auth`
CLI 역할을 호출한다. `api-database` 역할은 API DB runtime 파일만 만들며, 기존의
인자 없는 모듈 실행도 이 API DB 역할과 호환된다.
기본 Runner bootstrap은 PVC의 기존 `auth.json`을 보존하고 Secret Manager를 다시
읽지 않는다. OAuth 장애 복구 또는 계정 교체에서만 Runner CLI에
`--replace-existing`을 명시해 파일을 0600으로 원자 교체할 수 있다. 이 flag는
`api-database` 역할에서 허용되지 않는다.
