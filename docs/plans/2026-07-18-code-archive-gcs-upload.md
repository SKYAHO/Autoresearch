# 코드 아카이브 GCS 업로드 파이프라인 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** main 머지 시 저장소 추적 파일 전체를 `git archive`로 압축해 GCS에 SHA 불변 아카이브로 업로드하고 latest 포인터를 갱신하는 스크립트·워크플로우를 만든다.

**Architecture:** 로직은 `scripts/upload_code_archive.sh` 하나에 담고, 신규 워크플로우 `.github/workflows/code-archive.yml`(main push + workflow_dispatch, WIF 인증)이 이를 호출한다. 로컬 수동 실행과 CI가 같은 경로를 탄다.

**Tech Stack:** bash, `git archive`, `gcloud storage`, GitHub Actions (`google-github-actions/auth@v2` WIF 패턴 — `release.yml`과 동일).

**Spec:** `docs/specs/2026-07-18-code-archive-gcs-upload.md` · **이슈:** #174 · **브랜치:** `feat/174-code-archive-gcs-upload`

**사전 확인:** 현재 브랜치가 `feat/174-code-archive-gcs-upload`인지 `git branch --show-current`로 확인한다.

---

### Task 1: 업로드 스크립트 `scripts/upload_code_archive.sh`

**Files:**
- Create: `scripts/upload_code_archive.sh`

- [ ] **Step 1: 스크립트 작성**

아래 내용 그대로 `scripts/upload_code_archive.sh`를 생성한다.

```bash
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
```

- [ ] **Step 2: 실행 권한 부여**

Run: `chmod +x scripts/upload_code_archive.sh`

- [ ] **Step 3: 문법 검사**

Run: `bash -n scripts/upload_code_archive.sh`
Expected: 출력 없이 exit 0

- [ ] **Step 4: 필수 env 누락 실패 검증**

Run: `scripts/upload_code_archive.sh --dry-run; echo "exit=$?"`
Expected: stderr에 `오류: CODE_ARTIFACTS_BUCKET 환경 변수가 필요합니다`, `exit=2`

- [ ] **Step 5: dry-run 기본 동작 검증**

Run: `CODE_ARTIFACTS_BUCKET=dummy-bucket scripts/upload_code_archive.sh --dry-run`
Expected: `[dry-run] git archive --format=tar.gz <현재 HEAD 40자 SHA> -> gs://dummy-bucket/code/<SHA>.tar.gz` 한 줄만 출력 (latest 줄 없음)

- [ ] **Step 6: dry-run --update-latest 검증**

Run: `CODE_ARTIFACTS_BUCKET=dummy-bucket scripts/upload_code_archive.sh --update-latest --dry-run`
Expected: Step 5의 출력에 더해 `[dry-run] latest.txt(<SHA>) -> gs://dummy-bucket/code/latest.txt` 출력

- [ ] **Step 7: 알 수 없는 옵션 거부 검증**

Run: `CODE_ARTIFACTS_BUCKET=dummy-bucket scripts/upload_code_archive.sh --bogus; echo "exit=$?"`
Expected: stderr에 `오류: 알 수 없는 옵션: --bogus`와 usage, `exit=2`

- [ ] **Step 8: ref 인자 검증**

Run: `CODE_ARTIFACTS_BUCKET=dummy-bucket scripts/upload_code_archive.sh HEAD~1 --dry-run`
Expected: HEAD가 아닌 `HEAD~1`의 40자 SHA가 경로에 출력됨 (`git rev-parse HEAD~1`과 대조)

- [ ] **Step 9: shellcheck (로컬에 있으면)**

Run: `command -v shellcheck >/dev/null && shellcheck scripts/upload_code_archive.sh || echo "shellcheck 없음, 생략"`
Expected: 경고 없음 또는 생략 메시지

- [ ] **Step 10: 커밋**

```bash
git add scripts/upload_code_archive.sh
git commit -m "feat: 코드 아카이브 GCS 업로드 스크립트 추가 (#174)"
```

---

### Task 2: 워크플로우 `.github/workflows/code-archive.yml`

**Files:**
- Create: `.github/workflows/code-archive.yml`

- [ ] **Step 1: 워크플로우 작성**

아래 내용 그대로 `.github/workflows/code-archive.yml`을 생성한다.

```yaml
name: Code archive upload

on:
  push:
    branches: [main]
  workflow_dispatch:
    inputs:
      source_sha:
        description: Full 40-character commit SHA to archive (empty = dispatched ref HEAD)
        required: false
        type: string
      update_latest:
        description: Update code/latest.txt to this SHA
        required: false
        type: boolean
        default: false

permissions:
  contents: read

concurrency:
  group: code-archive
  cancel-in-progress: false

jobs:
  upload-code-archive:
    name: Upload code archive to GCS
    runs-on: ubuntu-latest
    permissions:
      contents: read
      id-token: write

    steps:
      - name: Checkout
        uses: actions/checkout@v6
        with:
          fetch-depth: 0

      - name: Resolve source ref and latest flag
        id: source
        env:
          EVENT_NAME: ${{ github.event_name }}
          MANUAL_SOURCE_SHA: ${{ inputs.source_sha }}
          MANUAL_UPDATE_LATEST: ${{ inputs.update_latest }}
          EVENT_SHA: ${{ github.sha }}
        shell: bash
        run: |
          if [[ "$EVENT_NAME" == "workflow_dispatch" ]]; then
            if [[ -z "$MANUAL_SOURCE_SHA" ]]; then
              ref="$EVENT_SHA"
            elif [[ "$MANUAL_SOURCE_SHA" =~ ^[0-9a-fA-F]{40}$ ]]; then
              ref="${MANUAL_SOURCE_SHA,,}"
            else
              echo "::error::source_sha must be a full 40-character commit SHA"
              exit 1
            fi
            update_latest="$MANUAL_UPDATE_LATEST"
          else
            ref="$EVENT_SHA"
            main_sha="$(git ls-remote --exit-code origin refs/heads/main | awk '{print $1}')"
            if [[ "$EVENT_SHA" == "$main_sha" ]]; then
              update_latest="true"
            else
              update_latest="false"
              echo "::notice::Skipping latest update: event SHA ${EVENT_SHA} is no longer main HEAD (${main_sha})"
            fi
          fi
          {
            echo "ref=$ref"
            echo "update_latest=$update_latest"
          } >> "$GITHUB_OUTPUT"

      - name: Authenticate to GCP with Workload Identity Federation
        uses: google-github-actions/auth@v2
        with:
          workload_identity_provider: ${{ vars.WIF_PROVIDER_ID }}
          service_account: ${{ secrets.GCS_CODE_UPLOADER_SA }}

      - name: Set up Cloud SDK
        uses: google-github-actions/setup-gcloud@v2

      - name: Upload code archive
        env:
          CODE_ARTIFACTS_BUCKET: ${{ secrets.CODE_ARTIFACTS_BUCKET }}
          SOURCE_REF: ${{ steps.source.outputs.ref }}
          UPDATE_LATEST: ${{ steps.source.outputs.update_latest }}
        shell: bash
        run: |
          args=("$SOURCE_REF")
          if [[ "$UPDATE_LATEST" == "true" ]]; then
            args+=("--update-latest")
          fi
          scripts/upload_code_archive.sh "${args[@]}"
```

- [ ] **Step 2: 공백 오류·워크플로우 lint 검사**

Run: `git diff --check && (command -v actionlint >/dev/null && actionlint .github/workflows/code-archive.yml || echo "actionlint 없음, 생략")`
Expected: 오류 없음 또는 생략 메시지

- [ ] **Step 3: 커밋**

```bash
git add .github/workflows/code-archive.yml
git commit -m "ci: main push 시 코드 아카이브 GCS 업로드 워크플로우 추가 (#174)"
```

---

### Task 3: 최종 검증·push·PR

**Files:** 없음 (검증·PR만)

- [ ] **Step 1: 전체 dry-run 재검증**

Run: `CODE_ARTIFACTS_BUCKET=dummy-bucket scripts/upload_code_archive.sh --update-latest --dry-run && git diff --check`
Expected: dry-run 두 줄 출력, diff 오류 없음

- [ ] **Step 2: push 및 PR 생성**

```bash
git push -u origin feat/174-code-archive-gcs-upload
gh pr create --base main \
  --title "feat: main 머지 시 코드 아카이브 GCS 업로드 파이프라인 (#174)" \
  --body "## 요약

- \`scripts/upload_code_archive.sh\`: git archive → \`gs://<bucket>/code/<sha>.tar.gz\` 업로드(멱등) → \`--update-latest\` 시 \`code/latest.txt\` 갱신. \`--dry-run\` 지원.
- \`.github/workflows/code-archive.yml\`: main push 자동(latest 갱신 포함) + workflow_dispatch 수동(SHA·latest 여부 지정), WIF 인증, concurrency로 latest 경합 방지.
- 설계: \`docs/specs/2026-07-18-code-archive-gcs-upload.md\` (소비자 계약 포함)

## 검증

- \`bash -n\`, \`--dry-run\` 시나리오(기본/latest/알 수 없는 옵션/ref 지정), \`git diff --check\`
- 실제 GCS 업로드는 secret(\`CODE_ARTIFACTS_BUCKET\`, \`GCS_CODE_UPLOADER_SA\`) 등록 후 workflow_dispatch로 확인 예정

## 후속 (이 PR 범위 밖)

- Autoresearch-infra: 전용 버킷·업로더 SA·WIF 바인딩 요청
- Dockerfile.feast 부트스트랩 전환(다운로드·압축 해제·커맨드 실행) 별도 이슈

Closes #174"
```

Expected: PR URL 출력

- [ ] **Step 3: 수동 후속 안내**

PR 머지 전 인프라 요청이 필수는 아님(secret 미등록 시 워크플로우가 인증 단계에서 실패할 뿐 main에 영향 없음)을 사용자에게 안내하고, Autoresearch-infra 요청(버킷·SA·WIF)과 GitHub secret 등록을 후속 작업으로 전달한다.

---

## Self-Review 체크

- Spec 커버리지: 레이아웃·멱등(§1→Task 1 Step 1), git archive 전체(§2→Task 1), 스크립트 계약(§3→Task 1 Step 4–8), 워크플로우 트리거·concurrency·WIF(§4→Task 2), 인프라 의존성(§5→Task 3 Step 3 안내), 검증(§검증→각 Task 검증 스텝). 소비자 계약(§6)은 문서 전용으로 코드 작업 없음.
- 산출물이 bash/YAML이라 pytest 대상 없음. 검증은 dry-run 시나리오·문법 검사·actionlint로 대체 (spec §검증과 일치).
