# Serving Image Release Implementation Plan

**Source design:** `docs/archive/specs/2026-07-23-serving-image-release-design.md`  
**Issue:** #266  
**Branch:** `feat/266-serving-image-release`

## Goal

Publish and verify an immutable `autoresearch-serving` image from the existing
release workflow without changing the existing batch image release path.

## Tasks

### Task 1: Add serving image source metadata

**Files:** `deploy/serving/Dockerfile`

- Add `ARG VCS_REF=unknown`.
- Add `org.opencontainers.image.revision` using `VCS_REF`.
- Preserve the existing Feast dependency export and `USER appuser` runtime.

### Task 2: Add the serving release job

**Files:** `.github/workflows/release.yml`

- Add `publish-serving-image` after the existing application image job.
- Reuse the existing job's verified `source_sha` output.
- Checkout that exact SHA and authenticate to GAR through the existing WIF
  variables and secret.
- Build and push `deploy/serving/Dockerfile` as
  `autoresearch-serving:sha-<source-sha>` plus the release tag when present.
- Pass `VCS_REF` to the build and verify the returned digest reference.
- Validate OCI revision, non-root user, and serving import smoke.
- Expose and summarize `digest_ref` for infra consumption.

### Task 3: Lock the release and Dockerfile contracts

**Files:** `tests/test_release_workflow.py`, `tests/test_serving_deployment.py`

- Assert the serving job, dependency, Dockerfile, push mode, build arg, and
  digest summary contract.
- Assert the Dockerfile metadata and existing serving runtime contracts.
- Keep tests static so they do not require GCP credentials or Redis.

### Task 4: Document the release handoff

**File:** `docs/guides/release-pipeline.md`

- Document both published images and their ownership boundary.
- Document serving image verification and the immutable digest handoff to the
  infra repository.

### Task 5: Validate and report

- Run targeted release and serving deployment tests.
- Run the full test suite and `git diff --check`.
- Record any pre-existing failures without altering unrelated work.
- Commit implementation separately from the design commit.
