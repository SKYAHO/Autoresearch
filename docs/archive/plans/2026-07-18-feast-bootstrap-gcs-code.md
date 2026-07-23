# Dockerfile.feast 부트스트랩 전환 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** feast 이미지에서 코드 COPY를 제거하고, 파드 시작 시 부트스트랩 스크립트가 GCS 코드 아카이브를 `/app`에 풀어 전달받은 커맨드를 실행하게 전환한다.

**Architecture:** `scripts/feast_bootstrap.sh`가 이미지 ENTRYPOINT가 된다. GCS 다운로드는 feast 그룹에 이미 있는 `google-cloud-storage` 파이썬 인라인 호출(ADC 자격 증명)로 수행하고, CI·로컬 검증은 `CODE_ARCHIVE_LOCAL_PATH` 주입 모드로 GCS 없이 압축 해제→실행 경로를 검증한다.

**Tech Stack:** bash, `google-cloud-storage`(기존 lock 포함), Docker, GitHub Actions.

**Spec:** `docs/specs/2026-07-18-feast-bootstrap-gcs-code.md` · **이슈:** #181 · **브랜치:** `feat/181-feast-bootstrap-gcs-code`

**사전 확인:** `git branch --show-current`가 `feat/181-feast-bootstrap-gcs-code`인지 확인. 로컬 docker 데몬은 꺼져 있을 수 있다 — Task 2 Step 3에서 확인 후 조건부 진행.

**핵심 주의(쿠버네티스 의미론):** 파드 spec의 `command:`는 docker ENTRYPOINT를 **덮어쓴다**. 부트스트랩을 타려면 파드는 `args:`만 지정해야 한다(Airflow KPO의 `cmds`도 동일). Task 4의 runbook 수정이 이를 반영한다.

---

### Task 1: 부트스트랩 스크립트 `scripts/feast_bootstrap.sh`

**Files:**
- Create: `scripts/feast_bootstrap.sh`

- [ ] **Step 1: 스크립트 작성**

아래 내용 그대로 `scripts/feast_bootstrap.sh`를 생성한다.

```bash
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

from google.api_core.exceptions import NotFound
from google.cloud import storage

bucket_name = os.environ["CODE_ARTIFACTS_BUCKET"]
sha = os.environ.get("CODE_ARCHIVE_SHA", "").strip()
bucket = storage.Client().bucket(bucket_name)
target = "code/latest.txt"
try:
    if not sha:
        sha = bucket.blob(target).download_as_text().strip()
    target = f"code/{sha}.tar.gz"
    bucket.blob(target).download_to_filename(sys.argv[1])
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
```

- [ ] **Step 2: 실행 권한 부여**

Run: `chmod +x scripts/feast_bootstrap.sh`

- [ ] **Step 3: 문법 검사**

Run: `bash -n scripts/feast_bootstrap.sh`
Expected: 출력 없이 exit 0

- [ ] **Step 4: 인자 없음 실패 검증**

Run: `scripts/feast_bootstrap.sh; echo "exit=$?"`
Expected: stderr에 `오류: 실행할 커맨드가 필요합니다.`와 사용법, `exit=2`

- [ ] **Step 5: env 없음 실패 검증**

Run: `env -u CODE_ARTIFACTS_BUCKET -u CODE_ARCHIVE_LOCAL_PATH scripts/feast_bootstrap.sh echo hi; echo "exit=$?"`
Expected: stderr에 `오류: CODE_ARTIFACTS_BUCKET 또는 CODE_ARCHIVE_LOCAL_PATH 환경 변수가 필요합니다`, `exit=2`

- [ ] **Step 6: 로컬 경로 부재 실패 검증**

Run: `CODE_ARCHIVE_LOCAL_PATH=/nonexistent.tar.gz scripts/feast_bootstrap.sh echo hi; echo "exit=$?"`
Expected: stderr에 `오류: CODE_ARCHIVE_LOCAL_PATH 파일이 없습니다: /nonexistent.tar.gz`, `exit=2`

(성공 경로는 `/app` 고정 경로 때문에 호스트에서 직접 실행할 수 없다 — Task 2의 docker 검증과 Task 3의 CI가 담당한다.)

- [ ] **Step 7: shellcheck (로컬에 있으면)**

Run: `command -v shellcheck >/dev/null && shellcheck scripts/feast_bootstrap.sh || echo "shellcheck 없음, 생략"`
Expected: 경고 없음 또는 생략 메시지

- [ ] **Step 8: 커밋**

```bash
git add scripts/feast_bootstrap.sh
git commit -m "feat: feast 파드 부트스트랩 스크립트 추가 (#181)"
```

---

### Task 2: `Dockerfile.feast` 슬림화

**Files:**
- Modify: `Dockerfile.feast` (33–38행: 코드 COPY 제거, ENTRYPOINT 전환)

- [ ] **Step 1: Dockerfile 수정**

`Dockerfile.feast` 전체를 아래 내용으로 교체한다 (1–31행은 기존 그대로, 33행 이후만 변경).

```dockerfile
FROM python:3.12-slim

ARG VCS_REF=unknown

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV AUTORESEARCH_REVISION=${VCS_REF}

# feast 0.64는 pyarrow>=21을 선언하지만 메인 의존성 해석은 pyarrow==19.0.1로
# 고정된다. uv.lock은 이 조합을 하나의 해로 고정하며 uv는 lock을 그대로
# 설치하므로 정상 동작한다. pip은 exported requirements를 재해석하며 feast의
# pyarrow>=21을 강제해 충돌하므로, 이 이미지는 pip이 아니라 uv로 lock 기반
# 설치한다.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy
ENV PATH="/opt/venv/bin:${PATH}"

LABEL org.opencontainers.image.source="https://github.com/SKYAHO/Autoresearch" \
      org.opencontainers.image.revision="${VCS_REF}" \
      io.autoresearch.batch-contract.version="batch-contract-v1"

COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /bin/uv

WORKDIR /app

RUN adduser --disabled-password --gecos "" appuser

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --group feast --no-install-project \
    && rm -rf /root/.cache/uv

# 코드는 이미지에 포함하지 않는다. ENTRYPOINT 부트스트랩이 파드 시작 시
# GCS 코드 아카이브(#174 파이프라인)를 /app에 풀고 커맨드를 실행한다.
# revision 라벨·AUTORESEARCH_REVISION은 이미지 빌드 시점 커밋을 뜻하며,
# 실행 코드 버전은 부트스트랩 로그([feast-bootstrap] code: ...)가 담당한다.
COPY scripts/feast_bootstrap.sh /usr/local/bin/feast_bootstrap.sh
RUN chown -R appuser /app

USER appuser

ENTRYPOINT ["/usr/local/bin/feast_bootstrap.sh"]
CMD ["python", "-c", "import feast, feature_repo.redis_iam; print('autoresearch feast image ready')"]
```

- [ ] **Step 2: 공백 오류 검사**

Run: `git diff --check`
Expected: 출력 없음

- [ ] **Step 3: 로컬 docker 검증 (데몬 실행 중일 때만)**

Run: `docker info >/dev/null 2>&1 && echo "docker 사용 가능" || echo "docker 데몬 없음 — CI 검증으로 대체"`

docker 사용 가능이면 다음을 실행한다:

```bash
docker build --build-arg VCS_REF="$(git rev-parse HEAD)" -f Dockerfile.feast -t autoresearch-feast:local .
git archive --format=tar.gz -o /tmp/code-archive.tar.gz HEAD
docker run --rm \
  -v /tmp/code-archive.tar.gz:/tmp/code-archive.tar.gz:ro \
  -e CODE_ARCHIVE_LOCAL_PATH=/tmp/code-archive.tar.gz \
  autoresearch-feast:local
```

Expected: `[feast-bootstrap] code: local:/tmp/code-archive.tar.gz` 다음 줄에 `autoresearch feast image ready`

이어서 env 없는 실패 경로:

```bash
docker run --rm autoresearch-feast:local; echo "exit=$?"
```

Expected: `오류: CODE_ARTIFACTS_BUCKET 또는 CODE_ARCHIVE_LOCAL_PATH 환경 변수가 필요합니다`, `exit=2`

docker 데몬이 없으면 이 스텝을 생략하고 보고에 명시한다 (Task 3의 CI가 동일 경로를 검증).

- [ ] **Step 4: 커밋**

```bash
git add Dockerfile.feast
git commit -m "feat: Dockerfile.feast에서 코드 COPY 제거·부트스트랩 ENTRYPOINT 전환 (#181)"
```

---

### Task 3: CI feast 이미지 검증을 로컬 아카이브 주입 모드로 전환

**Files:**
- Modify: `.github/workflows/ci.yml:129-135` (`Run Feast Docker image smoke check` 스텝)

- [ ] **Step 1: 스텝 교체**

`.github/workflows/ci.yml`의 `Run Feast Docker image smoke check` 스텝(현재 129–135행)을 아래로 교체한다. 직전의 `Build Feast Docker image` 스텝(120–127행)은 그대로 둔다.

```yaml
      - name: Run Feast Docker image smoke check
        # 코드가 이미지에 없으므로 로컬 아카이브 주입 모드로 부트스트랩
        # 압축 해제·실행 경로를 검증한다.
        run: |
          git archive --format=tar.gz -o /tmp/code-archive.tar.gz HEAD
          run_feast() {
            docker run --rm \
              -v /tmp/code-archive.tar.gz:/tmp/code-archive.tar.gz:ro \
              -e CODE_ARCHIVE_LOCAL_PATH=/tmp/code-archive.tar.gz \
              autoresearch-feast:ci "$@"
          }
          run_feast
          run_feast python -m autoresearch.jobs.feast_materialize --help
          run_feast python -m autoresearch.jobs.feast_materialize --version
          if docker run --rm autoresearch-feast:ci 2>/dev/null; then
            echo "::error::부트스트랩이 env 없이 성공해서는 안 된다"
            exit 1
          fi
```

- [ ] **Step 2: 검사**

Run: `git diff --check && (command -v actionlint >/dev/null && actionlint .github/workflows/ci.yml || echo "actionlint 없음, 생략")`
Expected: 오류 없음 또는 생략 메시지

- [ ] **Step 3: 커밋**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: feast 이미지 검증을 로컬 아카이브 주입 모드로 전환 (#181)"
```

---

### Task 4: GKE 검증 runbook 갱신

**Files:**
- Modify: `docs/runbooks/2026-07-15-feast-redis-gke-validation.md`

- [ ] **Step 1: 사전 변수에 코드 버킷 추가**

시작 변수 블록(`export BQ_DATASET=...`와 `export IMAGE=...` 사이)에 다음 줄을 추가한다:

```bash
export CODE_ARTIFACTS_BUCKET="<코드 아카이브 버킷 — Autoresearch-infra output 참조>"
```

- [ ] **Step 2: §1 이미지 빌드 절에 재빌드 조건 명시**

"## 1. feast 이미지 빌드·push (Cloud Build)" 제목 바로 아래 본문 첫 줄로 다음 문단을 추가한다:

```markdown
이미지는 의존성(`pyproject.toml`/`uv.lock`)이나 부트스트랩 스크립트가 바뀔
때만 재빌드하면 된다. 코드는 이미지에 포함되지 않고, 파드 시작 시
부트스트랩이 GCS 코드 아카이브를 받아 실행한다 (#181,
`docs/specs/2026-07-18-feast-bootstrap-gcs-code.md`).
```

- [ ] **Step 3: §3 pod 매니페스트 수정**

pod 매니페스트에서 `command: ["sleep", "7200"]`를 `args: ["sleep", "7200"]`로 바꾸고, env 목록 맨 앞에 `CODE_ARTIFACTS_BUCKET`을 추가한다. 해당 부분을 다음으로 교체한다:

```yaml
    - name: feast
      image: ${IMAGE}
      # command는 ENTRYPOINT(부트스트랩)를 덮어쓰므로 args만 지정한다.
      # Airflow KubernetesPodOperator에서도 cmds 대신 arguments를 사용한다.
      args: ["sleep", "7200"]
      env:
        - name: CODE_ARTIFACTS_BUCKET
          value: "${CODE_ARTIFACTS_BUCKET}"
        - name: GCP_PROJECT_ID
          value: "${PROJECT_ID}"
```

(이하 기존 env 항목 `BQ_DATASET`부터는 그대로 유지)

- [ ] **Step 4: 부트스트랩 로그 확인 절차 추가**

`kubectl wait --for=condition=Ready ...` 줄 바로 다음에 추가한다:

```bash
kubectl logs pod/feast-redis-validation -n "${NAMESPACE}" | grep feast-bootstrap
```

와 설명 한 줄:

```markdown
`[feast-bootstrap] code: <sha>`가 보이면 GCS 코드 주입이 성공한 것이다.
파드 GSA에 코드 버킷 `roles/storage.objectViewer`가 없으면 여기서 실패한다.
```

- [ ] **Step 5: 검사 및 커밋**

Run: `git diff --check`
Expected: 출력 없음

```bash
git add docs/runbooks/2026-07-15-feast-redis-gke-validation.md
git commit -m "docs: GKE 검증 runbook에 부트스트랩 코드 주입 반영 (#181)"
```

---

### Task 5: 최종 검증·push·PR

**Files:** 없음 (검증·PR만)

- [ ] **Step 1: 에러 경로 재검증**

Run: `scripts/feast_bootstrap.sh; echo "exit=$?"` → `exit=2` 확인
Run: `git diff --check` → 출력 없음

- [ ] **Step 2: push 및 PR 생성**

```bash
git push -u origin feat/181-feast-bootstrap-gcs-code
gh pr create --base main \
  --title "feat: Dockerfile.feast 부트스트랩 전환 — 파드 시작 시 GCS 코드 주입 (#181)" \
  --body "## 요약

- \`scripts/feast_bootstrap.sh\`: ENTRYPOINT. \`CODE_ARCHIVE_SHA\` 고정 또는 \`code/latest.txt\`로 SHA 확정 → GCS 다운로드(google-cloud-storage, ADC) → \`/app\` 압축 해제 → 실행 SHA 로그 → \`exec \"\$@\"\`. \`CODE_ARCHIVE_LOCAL_PATH\` 로컬 주입 모드 지원.
- \`Dockerfile.feast\`: 코드 COPY 제거(의존성 변경 시에만 재빌드), 부트스트랩 ENTRYPOINT 전환, \`/app\` appuser 소유.
- \`ci.yml\`: feast 이미지 검증을 로컬 아카이브 주입 모드로 전환 + env 없는 실패 경로 검증.
- runbook: pod \`command\` → \`args\` (command는 ENTRYPOINT를 덮어씀), 코드 버킷 env, 부트스트랩 로그 확인 절차.
- 설계: \`docs/specs/2026-07-18-feast-bootstrap-gcs-code.md\`

## 검증

- 스크립트 에러 경로(인자 없음/env 없음/로컬 경로 부재) 로컬 확인, \`bash -n\`, \`git diff --check\`
- CI가 빌드 후 로컬 아카이브 주입으로 압축 해제→실행 경로를 검증
- GCS 모드 E2E는 PR #180 머지 + 인프라(버킷·SA·파드 GSA objectViewer) 이후 runbook 절차로 수행

## 후속 (이 PR 범위 밖)

- Autoresearch-airflow: KPO에서 \`cmds\` 대신 \`arguments\` 사용 + env(\`CODE_ARTIFACTS_BUCKET\`, 선택 \`CODE_ARCHIVE_SHA\`) 전달
- Autoresearch-infra: 파드 GSA에 코드 버킷 \`roles/storage.objectViewer\`

Closes #181"
```

Expected: PR URL 출력

- [ ] **Step 3: 후속 안내**

사용자에게 전달: ① Airflow 쪽은 KPO `cmds`가 ENTRYPOINT를 덮어쓰므로 반드시 `arguments`를 사용해야 함, ② E2E는 #180 머지 + 인프라 프로비저닝 후 runbook §3–4로 검증, ③ CI 통과 여부 확인.

---

## Self-Review 체크

- Spec 커버리지: 부트스트랩 계약(§1→Task 1), Dockerfile 변경(§2→Task 2), CI 로컬 주입(§3→Task 3), runbook(§4→Task 4), 에러 처리(§1 에러→Task 1 Step 4–6 + Task 3 negative check), 검증(§검증→Task 2 Step 3·Task 3·Task 5). 경계·의존성은 PR 본문·후속 안내에 반영.
- 명칭 일관성: `feast_bootstrap.sh`, `CODE_ARTIFACTS_BUCKET`, `CODE_ARCHIVE_SHA`, `CODE_ARCHIVE_LOCAL_PATH`, `/usr/local/bin/feast_bootstrap.sh`, `autoresearch-feast:ci|local` — 전 태스크 동일.
- 산출물이 bash/Dockerfile/YAML/문서라 pytest 대상 없음. 기존 feast pytest(`tests/test_redis_iam.py` 등)는 이미지와 무관해 영향 없음.
