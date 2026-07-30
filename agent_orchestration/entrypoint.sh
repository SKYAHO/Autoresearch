#!/bin/sh
set -eu

runtime_env="${ORCH_RUNTIME_DIR}/db.env"

if [ ! -r "${runtime_env}" ]; then
  echo "Agent Orchestration DB runtime configuration is unavailable." >&2
  exit 1
fi

set -a
. "${runtime_env}"
set +a

exec uvicorn agent_orchestration.app.main:app --host 0.0.0.0 --port 8000
