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

# python이 Windows Store 별칭 스텁 등 진짜 인터프리터가 아니면 이후 elapsed_seconds
# 호출이 "Python"만 찍고 조용히 실패해 오염된 한 줄을 만든다(실측 확인됨). 미리 걸러낸다.
require_python() {
  if ! python -c 'import sys' >/dev/null 2>&1; then
    echo "오류: 'python' 명령이 정상 인터프리터가 아닙니다. PATH를 확인하십시오" \
      "(Windows에서는 Store 별칭 스텁일 수 있습니다 —" \
      "설정 > 앱 실행 별칭에서 python/python3를 끄고 실제 인터프리터를 PATH에 두십시오)." >&2
    exit 1
  fi
}

# started/completed 시각 하나를 초 단위 경과로 바꾼다. 입력이 비어 있거나 진행 중인 job의
# completed_at처럼 jq가 그대로 넘기는 문자열 "null"이면 strptime이 원인 불명의 traceback을
# 내며 죽는다 — 여기서 먼저 걸러 명확한 메시지와 함께 실패시킨다.
elapsed_seconds() {
  local started="$1" completed="$2"
  if [[ -z "$started" || -z "$completed" || "$started" == "null" || "$completed" == "null" ]]; then
    echo "오류: 시작/종료 시각이 비어 있거나 null입니다" \
      "(started='${started}', completed='${completed}'). job이 아직 진행 중일 수 있습니다." >&2
    return 1
  fi
  python - "$started" "$completed" <<'PY'
import sys
from datetime import datetime

started, completed = sys.argv[1], sys.argv[2]
fmt = "%Y-%m-%dT%H:%M:%SZ"
print(int((datetime.strptime(completed, fmt) - datetime.strptime(started, fmt)).total_seconds()))
PY
}

if [[ "$mode" == "before" ]]; then
  require_python
  echo "# release.yml feast 이미지 빌드 소요 (최근 ${limit}건)"
  gh run list --repo "$REPOSITORY" --workflow release.yml --limit "$limit" \
    --json databaseId -q '.[].databaseId' \
  | while read -r run_id; do
      gh api "repos/${REPOSITORY}/actions/runs/${run_id}/jobs" \
        --jq '.jobs[] | select(.name | test("feast image")) | "\(.started_at) \(.completed_at)"' \
      | while read -r started completed; do
          # 표준 명령문으로 먼저 대입해야 set -e가 실패를 감지한다 — printf 인자 자리에서
          # 바로 명령 치환을 하면 elapsed_seconds가 실패해도 스크립트가 멈추지 않고
          # 오염된 한 줄을 찍은 채 계속 진행한다(실측 확인됨).
          seconds="$(elapsed_seconds "$started" "$completed")"
          printf '%s\t%ss\n' "$run_id" "$seconds"
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
    # 방어적 분기: 정상 동작에서는 if: 게이트로 건너뛴 job도 GitHub가 conclusion="skipped"를
    # 명시적으로 돌려주므로 이 분기는 원래 도달하지 않는다. jq 결과가 완전히 비어 job 항목
    # 자체가 없는 비정상 상황(예: API 응답 지연·job 이름 변경)을 위한 보수적 기본값이다.
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
