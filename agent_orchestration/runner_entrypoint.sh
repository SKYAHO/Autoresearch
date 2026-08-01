#!/bin/sh
set -eu

exec uvicorn agent_orchestration.runner.app:app --host 0.0.0.0 --port 8080
