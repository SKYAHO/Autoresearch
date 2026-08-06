# 실험 candidate 이미지 조건부 빌드 — 의존성 diff 기반 이미지·코드 아카이브 결정

> 이슈: #560 | 작성: 2026-08-06

## 1. 배경

가설 실행은 파드 4개로 구성된다. Airflow를 쓰지 않고 launcher가 Kubernetes Job을
직접 순차 생성한다.

| 파드 | 역할 | 코드 |
|---|---|---|
| ① 코드 | codex가 이슈를 읽고 candidate 코드를 exp 브랜치에 push (`candidate_sha` 생성) | — |
| ② candidate | 데이터 조립 + candidate 코드로 학습 | `candidate_sha` |
| ③ baseline | ②가 만든 데이터셋 스냅샷을 그대로 읽어 학습 | `base_dev_sha` (항상 dev 코드) |
| ④ 판정 | `compare-paired-experiment`로 30쌍 비교 판정 | — |

①은 #546/#547 Phase 1으로 "브랜치 생성"까지 구현됐다
(`agent_orchestration/launcher/jobs.py`의 `build_branch_job()`).

**이 spec은 ② Job을 생성하기 직전, 어떤 이미지·어떤 코드로 돌 것인가를 결정하는
로직 하나만 다룬다.**

## 2. 문제

현재 부트스트랩 방식은 실행 환경과 코드를 분리한다.

- `Dockerfile.feast`는 `uv sync --frozen --no-dev --group feast`로 venv를 `/opt/venv`에
  굽고, **코드는 이미지에 넣지 않는다** (`Dockerfile.feast:38-42`)
- ENTRYPOINT `scripts/gcs_code_bootstrap.sh`가 파드 시작 시
  `gs://<bucket>/code/<sha>.tar.gz`를 받아 `/app`에 풀고 커맨드를 실행한다
- `scripts/upload_code_archive.sh <ref>`는 임의의 git ref를 올릴 수 있어
  `candidate_sha`에도 그대로 쓸 수 있다

문제는 **②candidate 파드에서만** 발생한다. candidate 코드가 `pyproject.toml`/`uv.lock`을
바꿔 새 라이브러리를 추가해도, venv는 이미지 빌드 시점에 고정되므로 부트스트랩이 코드만
갈아끼워서는 그 의존성이 설치되지 않는다.

③baseline 파드는 항상 `base_dev_sha` 코드로 돌므로 기존 dev 이미지를 그대로 쓰면 되고,
이 로직이 아예 필요 없다.

매 실험마다 새 이미지를 굽는 것은 비효율적이고, 애초에
`.github/workflows/release.yml`의 세 이미지(app/train/feast) 빌드는 전부
`git merge-base --is-ancestor "$source_sha" origin/main` 체크를 갖고 있어
(`release.yml:79`, `:1421`, `:1619`) main의 ancestor가 아닌 `candidate_sha`로는
이미지를 만들 수 없다.

## 3. 설계 결정

### 3.1 diff 비교 대상은 "이미지에 baking되는 파일" 전부

`Dockerfile.feast`가 이미지에 넣는 저장소 파일은 정확히 넷이다.

| 위치 | 내용 |
|---|---|
| `Dockerfile.feast:29` | `apt-get install libgomp1` — 파일이 아니라 Dockerfile 안의 `RUN` 줄 |
| `Dockerfile.feast:34` | `COPY pyproject.toml uv.lock ./` |
| `Dockerfile.feast:42` | `COPY scripts/gcs_code_bootstrap.sh /usr/local/bin/` |

(`Dockerfile.feast:21`의 `COPY --from=ghcr.io/astral-sh/uv:0.11.26`는 외부 이미지이므로
저장소 diff 대상이 아니다.)

따라서 비교 경로는 **4개**다.

```
pyproject.toml  uv.lock  Dockerfile.feast  scripts/gcs_code_bootstrap.sh
```

- `Dockerfile.feast` 자체를 넣는 이유: 새 모델이 pip 패키지만으로 안 되고 시스템
  라이브러리(apt)를 요구하면 candidate가 `Dockerfile.feast`에 설치 줄을 추가해야 한다.
  비교 대상이 lock 파일 둘뿐이면 이 변경이 조용히 무시된다. `libgomp1` 설치도
  `Dockerfile.feast` 안의 `RUN` 줄이므로 이 파일을 넣으면 함께 커버된다.
- `scripts/gcs_code_bootstrap.sh`를 넣는 이유: 이 스크립트는 **이미지에 COPY되는
  ENTRYPOINT**다. 부트스트랩이 갈아끼우는 것은 `/app`의 코드이지 이 스크립트가 아니므로,
  candidate가 고쳐도 이미지 안의 낡은 ENTRYPOINT가 계속 돈다. `Dockerfile.feast`와
  정확히 같은 실패 모드다.

경로 목록을 환경변수로 노출하지 않는다. 지금 이 넷은 "이미지 COPY 목록"이라는 구체적이고
검증 가능한 기준에서 나왔고, 설정으로 빼면 그 기준이 흐려지고 잘못 설정될 여지만 생긴다.

### 3.2 판단·실행을 GitHub Actions 러너에서 한다

launcher 파드에는 **git clone도 gcloud도 없다.** `launcher/main.py`는 in-cluster
Kubernetes API와 DB만 쓴다. `git diff`와 `upload_code_archive.sh`(= `git archive` +
`gcloud storage cp`)를 launcher에서 실행할 수 없다.

GitHub Actions 러너는 git·gcloud·WIF 인증·GAR push 권한이 이미 모두 있는 유일한
장소이므로, diff 판단과 아카이브 업로드와 조건부 빌드를 전부 러너에서 한다.
Python 쪽은 트리거와 결과 조회만 담당해 얇게 유지한다.

### 3.3 이미지 참조는 `exp-<candidate_sha>` 태그 — digest가 아니다

저장소 관례는 digest 고정이다 (`launcher/config.py`의 `_DIGEST_IMAGE_PATTERN`이
`executor_image`에 digest만 허용한다). 그럼에도 태그를 쓴다.

- **이유:** launcher에 GAR 읽기 권한이 없어 digest 해석에는 `Autoresearch-infra`의
  선행 변경(GSA 바인딩)이 필요하다. 발표 임계경로에 인접 저장소를 끌어들이지 않는다.
- **불변성 확보:** 태그가 `candidate_sha`당 1:1이고, 워크플로우가 **기존 태그
  덮어쓰기를 거부**한다(§4.2). 즉 태그를 썼다기보다 digest 고정과 실질적으로 동등하게
  만들었다.
- **전환 경로:** 나중에 GAR 읽기 권한이 생기면 `image_ref` 생성부만 digest로 바꾸면
  되고 **호출자 계약(`CandidateRuntime`)은 바뀌지 않는다.**

### 3.4 대상 이미지는 feast 하나

②candidate 파드는 `run-pipeline`(= `build-features` + `train-model`) 경로로 돈다.
#359 C2 이후 이 경로는 feast 이미지 소관이므로(`docs/guides/training-image.md`)
`Dockerfile.feast` 하나만 빌드한다. `Dockerfile.train`은 이 spec의 대상이 아니다.

### 3.5 빌드 완료는 비동기로 기다린다

launcher는 CronJob tick 구조라 tick을 블로킹하면 안 된다. 인터페이스는
`BUILD_PENDING`을 돌려주고 호출자가 다음 tick에 다시 묻는다. 기존 launcher 폴링
패턴(`launcher/main.py`의 `run_tick`, `launcher/jobs.py`의 `_is_terminal`)과 같은 모양이다.

## 4. 워크플로우 계약 — `.github/workflows/experiment-image.yml`

### 4.1 트리거와 식별

`workflow_dispatch` 전용.

| 입력 | 형식 |
|---|---|
| `base_dev_sha` | 40자 소문자 hex |
| `candidate_sha` | 40자 소문자 hex |

```yaml
run-name: experiment-image ${{ inputs.candidate_sha }}
```

이 `run-name`이 **호출자가 run을 찾는 유일한 키다.** 형식을 바꾸면 Python 쪽 조회가
깨진다. 조회 경로는 다음과 같다.

```
GET /repos/{repo}/actions/workflows/experiment-image.yml/runs?event=workflow_dispatch
```

응답 각 run의 `display_title`이 위 `run-name` 문자열과 정확히 일치하는 것을 찾고,
여럿이면 `created_at`이 가장 최근인 것을 쓴다.

### 4.2 job 계약

**job id `decide` (항상 실행)**

1. 입력 정규식 검증 — 어긋나면 즉시 실패
2. `candidate_sha` checkout (`fetch-depth: 0`)
3. **`origin/dev`와 `origin/exp/*`를 명시적으로 fetch한다**

   ```bash
   git fetch --no-tags origin \
     '+refs/heads/dev:refs/remotes/origin/dev' \
     '+refs/heads/exp/*:refs/remotes/origin/exp/*'
   ```

   `actions/checkout`에 `fetch-depth: 0`을 주면 히스토리는 전부 받아오지만 **다른
   브랜치의 remote-tracking ref까지 만들어주지는 않는다.** 이 fetch가 없으면 4의 guard가
   `origin/dev` unknown revision으로 터지거나 `git branch -r`이 빈 목록을 반환해 조용히
   어긋난다. `actionlint`로는 잡히지 않는 종류의 실수이므로 실측으로 확인한다(§7).

4. **provenance guard** — 제거하는 `merge-base --is-ancestor origin/main`의 대체물

   - `git merge-base --is-ancestor $base_dev_sha origin/dev`
     — baseline 기준은 항상 dev 코드라는 계약을 지킨다
   - `git branch -r --contains $candidate_sha`의 결과에 `origin/exp/`로 시작하는 항목이
     하나 이상 있어야 한다 — 명명 규칙은 `exp/<이슈번호>-<slug>`이며 원격에 실재한다
     (`exp/544-1`, `exp/558-lgbm-num-leaves-test-01` 등)

   둘 중 하나라도 어긋나면 실패한다. 임의의 ref로 실험 이미지를 굽는 경로를 막는다.

5. 의존성 diff 판단

   ```bash
   git diff --quiet "$base_dev_sha" "$candidate_sha" -- \
     pyproject.toml uv.lock Dockerfile.feast scripts/gcs_code_bootstrap.sh
   ```

   | exit code | 해석 |
   |---|---|
   | 0 | `dependencies_changed=false` |
   | 1 | `dependencies_changed=true` |
   | 그 외 | **실패 (fail-closed)** — 판단 불능을 "안 바뀜"으로 흘리지 않는다 |

6. WIF 인증 후 코드 아카이브 업로드

   ```bash
   CODE_ARTIFACTS_BUCKET=<bucket> scripts/upload_code_archive.sh "$candidate_sha"
   ```

   **`--update-latest`를 붙이지 않는다.** 붙이면 prod `code/latest.txt`가 실험 SHA로
   덮여, `CODE_ARCHIVE_SHA`를 지정하지 않는 모든 기존 파드가 실험 코드를 받게 된다
   (`scripts/gcs_code_bootstrap.sh:41-46`).

   출력: `dependencies_changed`

**job id `build-experiment-feast-image`**

- `needs: decide`
- `if: needs.decide.outputs.dependencies_changed == 'true'`
- **태그 선점 확인** — `<uri>:exp-<candidate_sha>`가 이미 있으면 빌드를 건너뛰고 성공
  종료한다. 덮어쓰기를 하지 않는 것이 §3.3 불변성의 근거다.
- 빌드: `file: Dockerfile.feast`, `tags: <uri>:exp-<candidate_sha>`,
  `build-args: VCS_REF=<candidate_sha>`
- verify: `release.yml`의 `publish-feast-image` job과 동일한 검사
  (`release.yml:1697-1738`) — digest 형식, OCI revision이 source SHA와 일치,
  non-root 사용자, `import feast, pyarrow, lightgbm, onnxmltools, onnxruntime, joblib, mlflow`
- 이미지 URI:
  `${GCP_REGION}-docker.pkg.dev/${GCP_PROJECT_ID}/${GAR_REPOSITORY}/autoresearch-feast`

**job 이름은 계약이다.** Python이 `GET /repos/{repo}/actions/runs/{id}/jobs` 응답에서
이 job을 이름으로 찾아 conclusion을 읽는다(§5.2). 이름을 바꾸면 판정이 깨진다.

이 API가 돌려주는 것은 job **id**가 아니라 표시 이름(`name` 필드)이다. `name:`을
지정하지 않으면 GitHub이 job id를 표시 이름으로 쓰지만, 그 암묵적 동작에 기대지 않고
두 job 모두 `name:`을 id와 **같은 문자열로 명시**한다.

```yaml
jobs:
  decide:
    name: decide
  build-experiment-feast-image:
    name: build-experiment-feast-image
```

### 4.3 prod 격리

| 수단 | 내용 |
|---|---|
| 태그 네임스페이스 분리 | 실험은 `exp-<sha>`, prod 릴리스는 `sha-<sha>` — 절대 섞이지 않는다 |
| 승격 job 부재 | `promote-airflow-digest-*` 계열 job을 **아예 포함하지 않는다.** 실험 이미지가 prod Airflow `values.yaml`로 새는 경로를 물리적으로 차단한다 |
| `latest.txt` 미갱신 | §4.2 6 |
| provenance guard | §4.2 4 |

`release.yml`은 수정하지 않는다.

## 5. Python 인터페이스 — `agent_orchestration/experiment_build/`

### 5.1 모듈 구성

`agent_orchestration/launcher/jobs.py`를 **직접 수정하지 않는다.** #557(실험 executor
Phase 2)의 영향 컴포넌트에 launcher Job 입력 계약이 포함돼 있어 같은 파일을 건드리면
충돌한다. `JobClient` 프로토콜 등 기존 유틸은 import해 재사용할 수 있다.

| 파일 | 책임 |
|---|---|
| `config.py` | `ExperimentBuildSettings` — `feast_image_uri`(GAR 저장소 경로), `dev_feast_image`(diff 없을 때 재사용할 dev 이미지 참조), `github_repository`, `workflow_file` |
| `contracts.py` | `ImageBuildState`, `CandidateRuntime` |
| `workflows.py` | `WorkflowRunClient` Protocol + `GitHubWorkflowRuns` 구현 |
| `service.py` | `resolve_candidate_runtime(...)` |

### 5.2 반환 계약

```python
class ImageBuildState(StrEnum):
    READY = "ready"
    BUILD_PENDING = "build_pending"
    BUILD_FAILED = "build_failed"


@dataclass(frozen=True)
class CandidateRuntime:
    state: ImageBuildState
    image_ref: str | None         # READY일 때만 채운다
    code_archive_sha: str | None  # READY일 때 항상 candidate_sha
```

판정 규칙 — **GitHub API만 사용한다. GAR·GCS 조회 권한이 필요 없다.**

| run 상태 | `build-experiment-feast-image` conclusion | `state` | `image_ref` |
|---|---|---|---|
| 없음 → dispatch | — | `BUILD_PENDING` | `None` |
| `queued` / `in_progress` | — | `BUILD_PENDING` | `None` |
| `completed` / `success` | `skipped` | `READY` | `settings.dev_feast_image` |
| `completed` / `success` | `success` | `READY` | `{settings.feast_image_uri}:exp-{candidate_sha}` |
| `completed` / `failure`·`cancelled`·`timed_out` | — | `BUILD_FAILED` | `None` |

**핵심:** "이미지를 실제로 구웠는가"를 job의 `skipped` vs `success`로 읽는다.
`GET /repos/{repo}/actions/runs/{id}/jobs`가 job 이름과 conclusion을 1급 필드로
반환하므로, 산출물(GAR 태그·GCS 객체) 조회 권한 없이 판별된다. 이 선택이 §3.3의
"digest 해석에 infra 권한이 필요하다"는 제약 자체를 비껴간다.

### 5.3 동작 규칙

- **dispatch 전에 항상 기존 run을 먼저 조회한다.** 같은 `candidate_sha`로 중복
  빌드하지 않는다 (멱등).
- SHA 형식 위반은 typed error로 즉시 실패한다 (fail-closed).
- GitHub 토큰은 **인자로 받는다** — `agent_orchestration/github_refs.py`와 같은 방식이다.
  이 모듈은 토큰을 발급하지 않는다.
- `github_app.py`/`github_refs.py`가 async httpx이므로 동일하게 async로 맞춘다.
  sync인 launcher tick은 `asyncio.run`으로 호출한다.
- `BUILD_FAILED`를 Experiment 상태(`ERROR` 등)로 매핑하는 것은 **호출자 책임**이다.

## 6. 경계

### 이 spec이 담당하지 않는 것

- ②③④ Job 빌더, 상태 머신 확장, `run_id` 수집, 판정 파드 실행
- **③baseline 파드의 이미지 결정** — 항상 고정된 dev feast 이미지 + `base_dev_sha`이므로
  이 인터페이스를 호출하지 않는다
- **launcher 파드에 GitHub 토큰을 주입하는 것** — token-minter init container 패턴
  (`launcher/jobs.py:86-116`)을 확장하는 작업이며 `Autoresearch-infra`가 얽힌다.
  ② Job 빌더 세션의 **선행 조건**이다.
- `src/pipeline/train.py`의 `convert_lgbm_to_onnx` LightGBM 전제. 새 라이브러리를 실행
  환경에 넣어주는 통로까지가 이 작업이고, 파이프라인이 그 라이브러리를 실제로 쓰도록
  코드를 바꾸는 것은 ①codex 몫이다. 라이브러리가 깔려도 학습 로직과 ONNX 변환이
  LightGBM 전용인 한 모델은 바뀌지 않는다.
- #557 (실험 executor Phase 2)

## 7. 검증

| 대상 | 방법 |
|---|---|
| 판정 로직 | run 상태 × build job conclusion 조합 전수를 fake `WorkflowRunClient`로 고정 |
| dispatch 멱등성 | 기존 run이 있으면 dispatch를 호출하지 않는다 |
| SHA 검증 | 형식 위반 입력이 typed error로 실패한다 |
| 워크플로우 문법 | `actionlint`, `git diff --check` |
| **fetch ref 존재** | 실제 두 SHA로 워크플로우를 한 번 돌려 `origin/dev`·`origin/exp/*`가 러너에 실재하는지 확인한다 — `actionlint`로 잡히지 않는다 |
| diff 판정 | 실제 두 SHA로 `git diff --quiet` 실측 (변경 있음/없음 양쪽) |
| 태그 덮어쓰기 거부 | 같은 `candidate_sha`로 워크플로우를 두 번 돌려 두 번째가 빌드를 건너뛰는지 확인 |
| 회귀 | `uv run python -m pytest` — baseline 68 failed / 2135 passed / 23 skipped 대비 증감으로 판단 |

## 8. 문서 갱신

`ExperimentBuildSettings`가 읽는 새 환경변수는 `.env.example`에 추가한다
(환경 변수의 단일 출처).

새 최상위 디렉토리·`Dockerfile.*`·공개 batch CLI를 도입하지 않으므로 `README.md`와
`.claude/docs/agent-project-reference.md`는 갱신 대상이 아니다.

측정 스크립트는 `scripts/bench/measure_experiment_image_build.sh`로 남긴다 —
Before 수치(§7의 회귀 baseline과 별개)는 `experiments/2026-08-06_experiment-conditional-image-build/`에
기록돼 있다.
