#!/usr/bin/env bash
# 파드 시작 시 코드 아카이브를 /app에 풀고 전달받은 커맨드를 실행한다.
# 계약: docs/specs/2026-07-18-feast-bootstrap-gcs-code.md
set -euo pipefail

if (( $# == 0 )); then
  cat >&2 <<'USAGE'
오류: 실행할 커맨드가 필요합니다.
사용법: feast_bootstrap.sh <command> [args...]
  env CODE_ARCHIVE_LOCAL_PATH  로컬 tar.gz 사용 (CI·로컬 검증용)
  env CODE_ARTIFACTS_BUCKET    코드 아카이브 GCS 버킷 이름 (gs:// 제외)
  env CODE_ARCHIVE_SHA         고정 실행할 40자 커밋 SHA (기본: code/latest.txt)
USAGE
  exit 2
fi

if [[ -n "${CODE_ARCHIVE_LOCAL_PATH:-}" ]]; then
  if [[ ! -f "${CODE_ARCHIVE_LOCAL_PATH}" ]]; then
    echo "오류: CODE_ARCHIVE_LOCAL_PATH 파일이 없습니다: ${CODE_ARCHIVE_LOCAL_PATH}" >&2
    exit 2
  fi
  archive_path="${CODE_ARCHIVE_LOCAL_PATH}"
  code_version="local:${CODE_ARCHIVE_LOCAL_PATH}"
else
  if [[ -z "${CODE_ARTIFACTS_BUCKET:-}" ]]; then
    echo "오류: CODE_ARTIFACTS_BUCKET 또는 CODE_ARCHIVE_LOCAL_PATH 환경 변수가 필요합니다" >&2
    exit 2
  fi
  archive_path="$(mktemp)"
  code_version="$(python - "${archive_path}" <<'PY'
import os
import sys

from google.api_core.exceptions import Forbidden, NotFound
from google.auth.exceptions import DefaultCredentialsError
from google.cloud import storage

bucket_name = os.environ["CODE_ARTIFACTS_BUCKET"]
sha = os.environ.get("CODE_ARCHIVE_SHA", "").strip()
target = "code/latest.txt"
try:
    bucket = storage.Client().bucket(bucket_name)
    if not sha:
        sha = bucket.blob(target).download_as_text().strip()
    target = f"code/{sha}.tar.gz"
    bucket.blob(target).download_to_filename(sys.argv[1])
except DefaultCredentialsError as exc:
    print(
        f"오류: GCP 자격 증명을 찾을 수 없습니다. "
        f"Workload Identity(KSA-GSA 바인딩) 또는 ADC 설정을 확인하세요: {exc}",
        file=sys.stderr,
    )
    sys.exit(2)
except Forbidden as exc:
    print(
        f"오류: gs://{bucket_name}/{target} 접근 권한이 없습니다. "
        f"파드 GSA에 해당 버킷 roles/storage.objectViewer가 필요합니다: {exc}",
        file=sys.stderr,
    )
    sys.exit(2)
except NotFound as exc:
    print(f"오류: gs://{bucket_name}/{target} 다운로드 실패: {exc}", file=sys.stderr)
    sys.exit(2)
print(sha)
PY
)"
fi

tar -xzf "${archive_path}" -C /app

if [[ -z "${CODE_ARCHIVE_LOCAL_PATH:-}" ]]; then
  rm -f "${archive_path}"
fi

echo "[feast-bootstrap] code: ${code_version}"
exec "$@"
