#!/bin/sh
set -eu

exec uvicorn applications.experiment_platform.runner.app:app --host 0.0.0.0 --port 8080
