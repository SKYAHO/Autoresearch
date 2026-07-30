#!/bin/sh
set -eu

# API 컨테이너는 API DB bootstrap이 작성한 런타임 파일만 사용한다.
database_env="${ORCH_RUNTIME_DIR:?ORCH_RUNTIME_DIR is required for the Agent Orchestration API container.}/db.env"

if [ ! -r "${database_env}" ]; then
  echo "Agent Orchestration DB runtime configuration is unavailable." >&2
  exit 1
fi

set -a
. "${database_env}"
set +a

exec uvicorn agent_orchestration.app.main:app --host 0.0.0.0 --port 8000
