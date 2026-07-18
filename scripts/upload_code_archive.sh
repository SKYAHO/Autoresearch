#!/usr/bin/env bash
# 저장소 추적 파일 전체를 git archive로 압축해 GCS에 업로드한다.
# 계약·경로 규칙: docs/specs/2026-07-18-code-archive-gcs-upload.md
set -euo pipefail

usage() {
  cat <<'USAGE'
사용법: CODE_ARTIFACTS_BUCKET=<bucket> scripts/upload_code_archive.sh [ref] [--update-latest] [--dry-run]

  ref              아카이브할 git ref (기본 HEAD)
  --update-latest  업로드 후 code/latest.txt를 이 SHA로 갱신
  --dry-run        gcloud 호출 없이 실행 계획만 출력
USAGE
}

ref="HEAD"
update_latest=0
dry_run=0

for arg in "$@"; do
  case "$arg" in
    --update-latest) update_latest=1 ;;
    --dry-run) dry_run=1 ;;
    -h|--help) usage; exit 0 ;;
    -*)
      echo "오류: 알 수 없는 옵션: $arg" >&2
      usage >&2
      exit 2
      ;;
    *) ref="$arg" ;;
  esac
done

if [[ -z "${CODE_ARTIFACTS_BUCKET:-}" ]]; then
  echo "오류: CODE_ARTIFACTS_BUCKET 환경 변수가 필요합니다" >&2
  exit 2
fi

sha="$(git rev-parse --verify "${ref}^{commit}")"
archive_uri="gs://${CODE_ARTIFACTS_BUCKET}/code/${sha}.tar.gz"
latest_uri="gs://${CODE_ARTIFACTS_BUCKET}/code/latest.txt"

if (( dry_run )); then
  echo "[dry-run] git archive --format=tar.gz ${sha} -> ${archive_uri}"
  if (( update_latest )); then
    echo "[dry-run] latest.txt(${sha}) -> ${latest_uri}"
  fi
  exit 0
fi

tmpdir="$(mktemp -d)"
trap 'rm -rf "$tmpdir"' EXIT

archive_path="${tmpdir}/${sha}.tar.gz"
git archive --format=tar.gz -o "${archive_path}" "${sha}"

if gcloud storage objects describe "${archive_uri}" >/dev/null 2>&1; then
  echo "이미 존재: ${archive_uri} (업로드 생략)"
else
  gcloud storage cp "${archive_path}" "${archive_uri}"
  echo "업로드 완료: ${archive_uri}"
fi

if (( update_latest )); then
  printf '%s\n' "${sha}" > "${tmpdir}/latest.txt"
  gcloud storage cp "${tmpdir}/latest.txt" "${latest_uri}"
  echo "latest 갱신: ${latest_uri} -> ${sha}"
fi
