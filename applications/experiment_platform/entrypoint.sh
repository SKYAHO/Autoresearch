#!/bin/sh
set -eu

# API 컨테이너는 API DB bootstrap이 작성한 런타임 파일만 사용한다.
database_env="${ORCH_RUNTIME_DIR:?ORCH_RUNTIME_DIR is required for the Agent Orchestration API container.}/db.env"

if [ ! -r "${database_env}" ]; then
  echo "Agent Orchestration DB runtime configuration is unavailable." >&2
  exit 1
fi

IFS= read -r database_env_line < "${database_env}" || true
case "${database_env_line}" in
  ORCH_DATABASE_URL=*)
    ORCH_DATABASE_URL="${database_env_line#ORCH_DATABASE_URL=}"
    export ORCH_DATABASE_URL
    ;;
  *)
    echo "Agent Orchestration DB runtime configuration is invalid." >&2
    exit 1
    ;;
esac

exec uvicorn applications.experiment_platform.api.main:app --host 0.0.0.0 --port 8000
