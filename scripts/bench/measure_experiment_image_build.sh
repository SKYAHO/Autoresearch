#!/usr/bin/env bash
# 실험 이미지 조건부 빌드의 재빌드 회피 효과를 측정한다.
# 기록 위치: experiments/2026-08-06_experiment-conditional-image-build/notes.md
set -euo pipefail

REPOSITORY="${REPOSITORY:-SKYAHO/Autoresearch}"
BUILD_JOB_NAME="build-experiment-feast-image"

usage() {
  cat <<'USAGE'
사용법: scripts/bench/measure_experiment_image_build.sh [--before|--after] [건수]

  --before  release.yml의 feast 이미지 빌드 job 소요 시간 (회피되는 비용)
  --after   experiment-image.yml의 재빌드 회피율
  건수      조회할 최근 run 수 (기본 5)
USAGE
}

mode=""
limit=5
for arg in "$@"; do
  case "$arg" in
    --before) mode="before" ;;
    --after) mode="after" ;;
    -h|--help) usage; exit 0 ;;
    *[!0-9]*)
      echo "오류: 알 수 없는 인자: $arg" >&2
      usage >&2
      exit 2
      ;;
    *) limit="$arg" ;;
  esac
done

if [[ -z "$mode" ]]; then
  usage >&2
  exit 2
fi

elapsed_seconds() {
  python - "$1" "$2" <<'PY'
import sys
from datetime import datetime

started, completed = sys.argv[1], sys.argv[2]
fmt = "%Y-%m-%dT%H:%M:%SZ"
print(int((datetime.strptime(completed, fmt) - datetime.strptime(started, fmt)).total_seconds()))
PY
}

if [[ "$mode" == "before" ]]; then
  echo "# release.yml feast 이미지 빌드 소요 (최근 ${limit}건)"
  gh run list --repo "$REPOSITORY" --workflow release.yml --limit "$limit" \
    --json databaseId -q '.[].databaseId' \
  | while read -r run_id; do
      gh api "repos/${REPOSITORY}/actions/runs/${run_id}/jobs" \
        --jq '.jobs[] | select(.name | test("feast image")) | "\(.started_at) \(.completed_at)"' \
      | while read -r started completed; do
          printf '%s\t%ss\n' "$run_id" "$(elapsed_seconds "$started" "$completed")"
        done
    done
  exit 0
fi

echo "# experiment-image.yml 재빌드 회피율 (최근 ${limit}건)"
total=0
skipped=0
while read -r run_id; do
  conclusion="$(gh api "repos/${REPOSITORY}/actions/runs/${run_id}/jobs" \
    --jq ".jobs[] | select(.name == \"${BUILD_JOB_NAME}\") | .conclusion")"
  if [[ -z "$conclusion" ]]; then
    conclusion="skipped"
  fi
  total=$((total + 1))
  if [[ "$conclusion" == "skipped" ]]; then
    skipped=$((skipped + 1))
  fi
  printf '%s\t%s\n' "$run_id" "$conclusion"
done < <(gh run list --repo "$REPOSITORY" --workflow experiment-image.yml \
  --limit "$limit" --json databaseId -q '.[].databaseId')

if (( total > 0 )); then
  printf '회피율: %d/%d (%d%%)\n' "$skipped" "$total" $(( skipped * 100 / total ))
else
  echo "run이 없습니다"
fi
