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

**이 예외는 실험 이미지에만 적용된다.** `dev_feast_image`는 이 워크플로우가 굽는 것이
아니라 `release.yml`이 이미 발행해 둔 dev 이미지이므로 digest를 호출자가 그대로 알 수
있다. 따라서 `ExperimentBuildSettings`는 `launcher/config.py`의 `_DIGEST_IMAGE_PATTERN`과
같은 형식(`<uri>@sha256:<64자리>`)을 `dev_feast_image`에 강제한다. `sha-<sha>` 태그는
`release.yml`이 재실행 때 같은 이름으로 다시 push하므로 가변이며 쓸 수 없다. 두 패키지의
설정 경계를 묶지 않기 위해 패턴은 import하지 않고 `experiment_build/config.py`에 따로
정의한다. **diff가 없는 경로가 다수 경로이므로 이 고정이 실제로 대부분의 실행에 적용된다.**

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
3. **`origin/dev`·`origin/exp/*`·`origin/main`을 명시적으로 fetch한다**

   ```bash
   git fetch --no-tags origin \
     '+refs/heads/dev:refs/remotes/origin/dev' \
     '+refs/heads/exp/*:refs/remotes/origin/exp/*' \
     '+refs/heads/main:refs/remotes/origin/main'
   ```

   `actions/checkout`에 `fetch-depth: 0`을 주면 히스토리는 전부 받아오지만 **다른
   브랜치의 remote-tracking ref까지 만들어주지는 않는다.** 이 fetch가 없으면 5의 guard가
   `origin/dev` unknown revision으로 터지거나 `git branch -r`이 빈 목록을 반환해 조용히
   어긋난다. `actionlint`로는 잡히지 않는 종류의 실수이므로 실측으로 확인한다(§7).
   `origin/main`은 4가 쓴다.

4. **신뢰 파일을 main 판으로 고정한다 (두 job 모두)**

   ```bash
   rm -rf .github/actions
   git checkout origin/main -- \
     scripts/upload_code_archive.sh .github/actions .dockerignore
   ```

   워크플로우 파일 자체는 `main`에서 dispatch되므로 신뢰되지만, **러너가 실행하는 것은
   candidate checkout의 파일들이다.** 그 상태로 WIF 자격증명을 러너에 올리면
   candidate가 쓴 코드가 자격증명을 쥔 채로 돈다. 구체적으로 `upload_code_archive.sh`는
   `--update-latest`를 스스로 붙여 prod `code/latest.txt`를 실험 SHA로 덮을 수 있고,
   `./.github/actions/*` composite action은 `gcloud auth configure-docker`가 적용된
   러너에서 임의 명령을 실행할 수 있으며, `.dockerignore`는 `auth@v2`가
   `GITHUB_WORKSPACE`에 쓰는 `gha-creds-*.json`을 `context: .` 빌드에서 제외하는 유일한
   장치다(이 파일에서 그 줄을 지우고 `Dockerfile.feast`에 `COPY`를 더하면 GAR pusher
   자격증명이 push된 레이어로 새어 나가는데, `Dockerfile.feast` 변경이 바로 빌드 job을
   트리거하는 조건이다). §4.2 5의 provenance guard는 이를 막지 못한다 — guard가 증명하는
   것은 `origin/exp/*`에서 도달 가능하다는 사실뿐이고, 코드 에이전트가 push하는 곳이
   바로 그 브랜치이기 때문이다.

   따라서 두 job 모두 **candidate checkout 직후, `google-github-actions/auth`와
   `./.github/actions/*` 사용 이전에** 이 고정을 수행한다. 고정이 실패하면 러너에 어떤
   자격증명도 올라오기 전에 job이 끝난다. `.github/actions`는 candidate가 파일을
   *추가*하는 경우까지 지우기 위해 복원 전에 통째로 삭제한다.

   - **`Dockerfile.feast`는 고정하지 않는다.** candidate가 이 파일을 바꿔 시스템
     라이브러리를 추가하는 것이 이 기능의 목적이다(§3.1).
   - **업로드되는 아카이브는 여전히 candidate의 것이다.** `upload_code_archive.sh`는
     `git archive --format=tar.gz -o … "${sha}"`로 **커밋 객체의 트리**를 압축하므로,
     작업 트리 파일을 되돌려도 아카이브 내용은 바뀌지 않는다.
   - 빌드 job의 checkout은 `fetch-depth: 1`이라 `origin/main`을 이 job이 직접
     `git fetch --no-tags --depth=1`로 받아야 한다. 얕은 클론에서도
     `git checkout origin/main -- <paths>`는 그대로 동작한다(실측 확인).

5. **provenance guard** — 제거하는 `merge-base --is-ancestor origin/main`의 대체물

   - `git merge-base --is-ancestor $base_dev_sha origin/dev`
     — baseline 기준은 항상 dev 코드라는 계약을 지킨다
   - `git branch -r --contains $candidate_sha`의 결과에 `origin/exp/`로 시작하는 항목이
     하나 이상 있어야 한다 — 명명 규칙은 `exp/<이슈번호>-<slug>`이며 원격에 실재한다
     (`exp/544-1`, `exp/558-lgbm-num-leaves-test-01` 등)

   둘 중 하나라도 어긋나면 실패한다. 임의의 ref로 실험 이미지를 굽는 경로를 막는다.

6. 의존성 diff 판단

   ```bash
   git diff --quiet "$base_dev_sha" "$candidate_sha" -- \
     pyproject.toml uv.lock Dockerfile.feast scripts/gcs_code_bootstrap.sh
   ```

   | exit code | 해석 |
   |---|---|
   | 0 | `dependencies_changed=false` |
   | 1 | `dependencies_changed=true` |
   | 그 외 | **실패 (fail-closed)** — 판단 불능을 "안 바뀜"으로 흘리지 않는다 |

7. WIF 인증 후 코드 아카이브 업로드

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
- **신뢰 파일 고정** — §4.2 4와 같다. checkout 직후, 어떤 인증 스텝보다 먼저 수행한다.
- **태그 선점 확인** — `<uri>:exp-<candidate_sha>`가 이미 있으면 빌드를 건너뛰고 성공
  종료한다. 덮어쓰기를 하지 않는 것이 §3.3 불변성의 근거다.

  판별은 **gcloud 오류 문구가 아니라 종료 코드와 출력 유무로만** 한다.

  ```bash
  matches="$(gcloud artifacts docker images list "$IMAGE_URI" \
    --include-tags --filter="tags:exp-${CANDIDATE_SHA}" --format='value(version)')"
  ```

  | 결과 | 해석 |
  |---|---|
  | exit 0 + 출력 없음 | 태그 없음 → `exists=false` (빌드한다) |
  | exit 0 + 출력 있음 | 태그 있음 → `exists=true` (건너뛴다) |
  | exit ≠ 0 | **판별 불가 → 실패 (fail-closed)** |

  `describe`의 실패 메시지를 `grep`으로 매칭하지 않는 이유: 그 문구는 gcloud 판마다
  다르고(`Image not found`, `NOT_FOUND`, `Failed to describe image` …), 매칭이 어긋나면
  판별 불가로 흘러 **모든 신규 candidate의 첫 빌드가 실패한다.** `list`는 태그가 없어도
  0으로 끝나므로 세 상태가 문구 없이 갈린다. GitHub Actions의 `shell: bash`는
  `-eo pipefail`로 실행되고 `var="$(cmd)"` 단독 대입도 errexit 대상이므로, `$?`를 읽으려면
  `set +e`/`set -e` 구간이 여전히 필요하다.
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
| `latest.txt` 미갱신 | §4.2 7 |
| provenance guard | §4.2 5 |
| **신뢰 파일 main 고정** | `scripts/upload_code_archive.sh`·`.github/actions`·`.dockerignore`를 두 job 모두에서 `origin/main` 판으로 되돌린다. candidate가 쓴 코드가 WIF 자격증명을 쥔 러너에서 도는 경로를 끊는다 — §4.2 4 |

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

### 알려진 한계 (의도적으로 감수한다)

- **provenance guard는 "그 브랜치에서 도달 가능한 커밋"까지 통과시킨다.**
  `git branch -r --contains $candidate_sha`는 `candidate_sha`가 exp 브랜치의 *조상*이기만
  해도 통과하므로, exp 브랜치를 딴 시점의 dev 커밋(사실상 `base_dev_sha`)도 통과할 수
  있다. "무관한 임의 ref 차단"이라는 목적은 달성하지만 "이것이 codex가 만든 candidate
  끝점 커밋인가"까지는 보장하지 않는다. 이 워크플로우를 호출하는 주체가 신뢰된 내부
  컴포넌트뿐이므로 현재 스코프에서는 감수한다. 예상 밖의 SHA로 이미지가 구워지는
  증상이 나오면 이 지점을 먼저 의심한다.
- **dispatch 전 조회와 dispatch 사이에 TOCTOU 경합이 있다.**
  §5.3의 멱등성은 순차 호출을 전제한다. launcher tick이 겹치지 않는 CronJob 구조라
  현재 위험은 낮지만, 여러 실험이 이 인터페이스를 병렬 호출하게 되면 같은
  `candidate_sha`로 두 번 dispatch될 수 있다. §7 테스트는 "기존 run이 있으면 dispatch하지
  않는다"만 고정하며 이 경합 자체는 검증하지 않는다.

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
| 신뢰 파일 고정 | 두 job 모두에 고정 스텝이 있고 `auth`·`./.github/actions/*`보다 앞선다는 것을 스텝 인덱스로 고정 (`tests/test_experiment_build_workflow.py`) |
| 태그 존재 판별 | 종료 코드 기반 3분기 형태를 고정 — 문구 매칭·2분기 fail-open으로 되돌리면 깨진다 |
| 프로토콜 적합성 | `GitHubWorkflowRuns`의 세 메서드 시그니처를 `WorkflowRunClient`와 `inspect.signature`로 대조 (이 저장소에는 타입 검사기가 없다) |
| 회귀 | `uv run python -m pytest` — baseline 68 failed / 2135 passed / 23 skipped 대비 증감으로 판단 |

### 7.1 첫 실행 전제 조건 (아직 충족 여부가 확인되지 않았다)

**위 표에서 러너에서만 확인 가능한 항목(fetch ref 존재, diff 판정, 태그 덮어쓰기 거부)은
아직 하나도 수행되지 않았다.** `actionlint`는 개발 환경 PATH에 없었고 이 워크플로우는
한 번도 실행된 적이 없다. 정적 검증(`yaml.safe_load` 재파싱, 모든 `run` 블록의 `bash -n`,
`git diff --check`, pytest)만 통과한 상태다. 다음 세션이 이 인터페이스에 의존하기 전에
실제 `exp/*` 쌍으로 `main`에서 한 번 돌려 확인해야 한다.

첫 dispatch 전에 반드시 확인할 것.

1. **워크플로우 파일이 기본 브랜치에 있어야 한다.**
   `POST /actions/workflows/{file}/dispatches`는 기본 브랜치의 워크플로우 정의를 찾고,
   클라이언트 기본값 `workflow_ref: "main"`도 `main`에서 해석된다. 이 브랜치에만 있는
   동안에는 dispatch가 404로 실패한다.
2. **WIF pool의 `attribute_condition`이 이 워크플로우 파일을 허용하는지 확인해야 한다.**
   `docs/guides/release-pipeline.md`(인프라 리포 표, `terraform/bootstrap/main.tf` 행)에
   WIF pool이 `attribute_condition`(list 멤버십)을 갖는다고 기록돼 있다. 그 목록이 *새*
   워크플로우 파일을 받아들이는지는 이 저장소만 봐서는 알 수 없다 — 조건은
   `SKYAHO/Autoresearch-infra` 소유다. **첫 dispatch 전에 인프라 소유자에게 확인해야
   하며, 확인 없이 돌리면 두 job의 `google-github-actions/auth@v2`가 모두 실패한다.**
3. `GAR_REPOSITORY`에 `autoresearch-feast` 패키지가 이미 있어야 한다. §4.2의 태그 선점
   확인은 패키지 자체가 없으면 `list`가 non-zero로 끝나 fail-closed로 막는다. `release.yml`이
   같은 좌표로 이 이미지를 발행해 왔으므로 통상 충족되지만, 새 GAR 저장소를 쓰기 시작하면
   첫 실행이 여기서 막힌다.
4. **태그 선점 확인의 stdout 가정을 첫 실행에서 눈으로 확인한다.** 이 검사는 두 가지를
   가정하는데 안전 방향이 서로 다르다. *종료 코드* 가정이 틀리면 fail-closed(빌드가 막힘)
   지만, *stdout* 가정이 틀리면 **fail-open**이다 — 어떤 gcloud 버전이 `Listed 0 items.`
   류의 상태 줄을 stderr가 아니라 stdout으로 보내면 `matches`가 비어 있지 않게 되어 없는
   태그를 "있다"로 판정하고, 빌드를 건너뛴 채 존재하지 않는 `exp-<sha>` 참조를 파드에
   넘긴다. gcloud는 상태·로그를 stderr로, `--format` 출력만 stdout으로 보내므로 통상
   문제되지 않지만, 이 방향만은 정적으로 확정할 수 없다. 태그가 없는 candidate로 첫
   실행을 돌려 `exists=false`가 실제로 나오는지 확인한다.

## 8. 문서 갱신

`ExperimentBuildSettings`가 읽는 새 환경변수는 `.env.example`에 추가한다
(환경 변수의 단일 출처).

새 최상위 디렉토리·`Dockerfile.*`·공개 batch CLI를 도입하지 않으므로 `README.md`는
갱신 대상이 아니다. 반면 `.claude/docs/agent-project-reference.md`는 **갱신 대상이다.**
이 파일은 `agent_orchestration/` 절에 기능별 `ORCH_*` 환경 변수 레지스트리를 유지한다
(#516 블록, #546 블록 — 후자는 digest-only `ORCH_EXECUTOR_IMAGE`를 명시한다). 새 설정
둘(`ORCH_EXPERIMENT_FEAST_IMAGE_URI`, digest-only `ORCH_DEV_FEAST_IMAGE`)도 같은 관례로
그 절에 추가한다. 기본값·형식의 정본은 `.env.example`이라는 표기도 함께 따른다.

측정 스크립트는 `scripts/bench/measure_experiment_image_build.sh`로 남긴다. Before 수치는
저장소에 커밋하지 않는다 — 관례상 유지자 체크아웃의 추적되지 않는 작업 공간
`experiments/<날짜>_<슬러그>/`에 기록하며, **이 저장소에서 조회할 수 있는 산출물이
아니다** (§7의 회귀 baseline과도 별개다).
