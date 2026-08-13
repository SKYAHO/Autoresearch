# 실험 executor Phase 2 — 이슈 기반 코드 수정과 candidate commit

> 상태: Phase 2 구현 완료, #565 Codex 인증 Secret 전환 구현 완료, #592 입력 경계 단순화 승인
> 관련 이슈: #557, #582, #592
> 선행 계약: #546, `2026-08-05-experiment-job-baseline-freeze.md`

## 목적

Phase 1은 `Experiment`에 봉인된 `base_dev_sha`에서
`exp/<issue>-<slug>` branch ref를 생성한다. Phase 2는 그 브랜치를 격리
workspace에 준비하고, Codex가 이슈 범위의 코드를 수정·검증하게 한 뒤,
executor가 유효한 변경만 candidate commit으로 만들어 같은 브랜치에 push한다.
확인된 commit SHA는 `candidate_sha`로 저장하고 실행 중 (`RUNNING`) → 평가 중
(`EVALUATING`) 인계까지 완료한다.

핵심 원칙은 다음과 같다.

> 코드의 의미를 판단하는 작업은 Codex가, Git·계보·자격증명·상태 전이처럼 결과가
> 결정론적이어야 하는 작업은 executor 코드가 담당한다.

## 범위

### 포함

- DB가 지정한 실행 좌표와 GitHub 이슈 원문 전달
- 기존 exp 브랜치 clone·checkout
- Codex workspace-write 실행과 코드 수정
- executor의 변경 범위·전체 테스트 재검증
- 결정론적 commit metadata와 exp 브랜치 push
- 원격 branch tip 확인, `candidate_sha` 저장, 평가 중 (`EVALUATING`) 전이
- Pod 재시도와 push 이후 상태 보고 실패 복구
- executor image와 인접 Infra 배포 계약

### 제외

- 모델 학습, 데이터셋 조립, paired 다중 시드 평가와 결과 판정
- 평가 중 (`EVALUATING`) → 통과/실패 (`PASSED`/`FAILED`) 전이
- `dev`·`main` 반영, PR 생성과 merge
- 실험 결과 코멘트와 이슈 본문 갱신
- Airflow DAG와 후속 평가 파이프라인 구현

## 현재 상태와 호환성

- Phase 1 launcher는 생성됨 (`CREATED`) Experiment를 실행 중 (`RUNNING`)으로 선점하고 결정론적
  Kubernetes Job을 만든다.
- Phase 1 executor는 ref 조회·생성만 수행하며 clone, Codex, commit, push를 하지
  않는다.
- Phase 2는 **새로 선점되는 Experiment의 기존 Job을 end-to-end executor Job으로
  확장**한다. 별도 Phase 2 Job이나 Job 간 workspace 전달 저장소를 만들지 않는다.
- 배포 전에 이미 실행 중 (`RUNNING`)이고 Phase 1 Job 생성 확인까지 끝난 행은 자동으로
  재실행하지 않는다. #554 smoke 행도 Phase 1 증거로 보존하고 Phase 2 smoke는 새
  Experiment로 수행한다.
- Phase 1의 `base_dev_sha`, `issue_number`, `issue_branch`, 동일 SHA branch 생성
  멱등성은 그대로 유지한다.

## 책임 경계

### Executor 코드

- DB에서 복사된 실행 좌표를 사용한다.
- GitHub 이슈 원문과 ref를 조회하고 workspace를 clone·checkout한다.
- Codex와 고정 verifier를 순서대로 실행한다.
- diff와 검증 산출물을 독립적으로 검사한다.
- commit·push하고 원격 tip을 확인한다.
- candidate API에 `candidate_sha`를 보고한다.

### Codex

- GitHub 이슈 원문을 해석한다.
- 변경 위치와 구현 방법을 판단한다.
- workspace 파일을 수정한다.
- 관련 테스트를 선택·실행하고 실패를 분석해 보완한다.
- Git ref 생성, commit, push, 상태 전이를 수행하지 않는다.

### Autoresearch-infra

- Job의 container 순서, container별 Secret·volume mount, KSA, admission,
  NetworkPolicy, resource와 deadline을 소유한다.
- executor KSA에는 Kubernetes API 권한을 부여하지 않는다.

## 단일 Job 실행 구조

하나의 Pod에서 initContainer를 순서대로 실행하고 마지막 main container가 candidate를
확정한다. 실행 단계는 같은 `emptyDir` workspace를 공유한다. workspace-preparer는 별도
executor-state `emptyDir`에 GitHub 이슈 본문·고정 scope·base·remote tip을 canonical JSON으로
기록하고, 이후 단계는 이를 read-only로 mount한다. 자격증명 volume은 필요한 container에만
mount한다.

```text
1. branch-token-minter
   GitHub App private key mount
   → Contents: Write token 발급

2. branch-creator
   branch token mount
   → base_dev_sha에서 exp branch 생성
   → 이미 존재하면 변경하지 않고 원격 tip을 후속 검증으로 전달

3. clone-token-minter
   GitHub App private key mount
   → Issues: Read + Contents: Read token 발급

4. workspace-preparer
   clone token + workspace mount
   → GitHub 이슈 본문 조회(내용 파싱 없음)
   → exp branch clone·checkout
   → HEAD == 원격 tip 확인, base 또는 기존 candidate 경로 분기

5. codex-worker
   workspace + executor 전용 CODEX_HOME Secret mount
   GitHub/API token volume 미마운트
   → 이슈 해석, 코드 수정, 선택 테스트, 실패 보완

6. candidate-verifier
   workspace만 mount
   GitHub/API/Codex credential 미마운트
   → 범위 검사, diff, 전체 pytest, Ruff

7. push-token-minter
   GitHub App private key mount
   → Contents: Write token을 새로 발급

8. candidate-finalizer (main)
   workspace + push token + executor API token mount
   → commit·push·원격 tip 재조회
   → candidate_sha 저장 및 평가 중 (EVALUATING) 전이
```

`branch-creator`는 기존 Phase 1의 실험 브랜치 생성 역할을 이름 그대로 표현한 단계다.
Phase 2가 별도 Job을 만들지 않고 기존 Job을 확장하므로 새 Experiment에서도 이 단계를
유지해야 한다. branch·clone·push token을 분리하면 각 executor 단계가 필요한 최소 권한만
사용하고, Codex 실행 시간이 길어져도 push 시점에 새 token을 사용할 수 있다.
Codex·verifier는 어떤 GitHub token volume도 볼 수 없으며 private key는 세
token-minter에만 read-only mount한다.

## 입력과 이슈 원문 전달 (#592)

launcher는 DB에 저장된 다음 실행 좌표를 literal env로 전달한다.

- `ORCH_EXPERIMENT_ID`
- `ORCH_ISSUE_NUMBER`
- `ORCH_ISSUE_BRANCH`
- `ORCH_BASE_DEV_SHA`
- `ORCH_GITHUB_REPOSITORY`

workspace-preparer는 `ORCH_ISSUE_NUMBER`의 GitHub 이슈 본문을 그대로 읽고,
`ORCH_ISSUE_BRANCH`를 clone·checkout한다. 이 단계는 Issue Form heading, 지표,
Guardrail, 데이터셋, 시드, 허용 범위, marker, 본문 hash를 파싱하거나 검증하지 않는다.
Issue template은 사용자 입력 UI이고 executor 실행 계약이 아니다.

실행 전 필요한 조건은 다음뿐이다.

1. 설정된 repository에서 `issue_number`를 읽을 수 있다.
2. `issue_branch`를 clone·checkout할 수 있다.
3. checkout HEAD가 조회한 원격 branch tip과 같다.

Codex prompt는 이슈 본문 원문, executor가 고정한 허용·금지 경로와 필수 검증
명령을 포함한다. 이슈 원문은 작업 요구사항일 뿐 실행 경계나 권한을 바꾸지 않는다.
실행 지시의 정본은 발행 시 DB에 저장한 사본이 아니라 **실행 시점의 GitHub 현재
본문**이다. executor는 본문 hash, credential 문자열, 내부 endpoint를 의미 검사하지
않으므로 사용자는 이슈에 Secret을 붙여 넣지 않아야 한다. candidate에 기록된 credential은
기존 verifier가 계속 거부한다.

## Workspace와 Git 계약

- workspace는 Pod별 `emptyDir`이며 Job 종료와 함께 삭제한다.
- clone은 clean URL과 일회성 `GIT_ASKPASS`를 사용한다. token을 URL,
  `.git/config`, credential helper에 저장하지 않는다.
- checkout 결과는 정확히 `issue_branch`의 원격 tip이어야 한다. 원격 tip이
  `base_dev_sha`일 때만 Codex를 실행한다. 다른 SHA면 ref를 변경하지 않고 Codex를 건너뛴
  뒤 기존 candidate 채택 조건을 verifier와 finalizer가 검사한다.
- Codex 실행 전후 `.git`은 read-only로 유지하고 executor가 remote URL,
  `core.hooksPath`, credential helper와 ref를 다시 확인한다.
- commit과 push는 candidate-finalizer만 수행한다. hook은 비활성화하고
  `issue_branch` 이외 refspec을 거부한다.
- force-push, ref 삭제, `main`·`dev`·다른 `exp/*` push는 구현 경로에 두지 않는다.

## Codex 실행 계약

- 비대화식 `codex exec --sandbox workspace-write`를 사용한다.
- `-C`는 준비된 repository root로 고정하고 git repository 검사를 생략하지 않는다.
- subprocess 환경은 명시적 allowlist로 새로 구성한다.
- 허용 환경은 `CODEX_HOME`, 임시 HOME/XDG/TMP, PATH, locale과 executor image가 고정한
  `UV_PROJECT_ENVIRONMENT=/opt/autoresearch-venv`로 제한한다.
- GitHub App, executor API, Kubernetes, GCP 자격증명과 상위 process의 전체 환경은
  전달하지 않는다.
- Codex stdout/stderr 원문은 영속 로그로 저장하지 않는다. 종료 코드, 정제 사유,
  제한된 응답 요약과 선택 테스트 결과만 남긴다.
- timeout·취소 시 process group을 종료하고 candidate 생성 없이 실패한다.

## 변경 허용 범위

기본 수정 가능 경로는 다음으로 제한한다.

- `autoresearch/**` (`autoresearch/feature_engineering/model_contract.py` 제외)
- `src/**` — #754 재배치 **이전**에 봉인된 트리에만 존재한다. 옛 봉인 SHA 실험이
  모두 끝나면 제거한다. 정본은 `executor/prompt.py`와 `executor/verifier.py`이며,
  두 곳 모두 워크스페이스 트리를 보고 판단한다
- `autoresearch/**`, `tests/**`, `tools/**`

MVP에서는 Issue Form 내용으로 수정 범위를 확장하지 않는다. 추가 범위가 필요하면
Issue heading이 아니라 executor 코드와 verifier 계약을 별도 변경한다.
의존성 추가·변경(`pyproject.toml`, `uv.lock`)은 verifier image의 봉인된 실행 환경과
일치시킬 수 없으므로 Phase 2 candidate 범위에서 제외한다.

다음은 항상 금지한다.

- `.git/**`, `.github/**`, `.claude/**`, `docs/**`
- `deploy/**`, `proxy/**`, `agent_orchestration/**`
- `.env`, `.env.*` (`.env.example` 포함)
- private key, token, credential, 생성 데이터와 로컬 절대 경로를 포함한 파일
- symlink, submodule, Git LFS pointer의 신규 추가 또는 변경

범위를 벗어난 변경은 일부만 버리지 않고 candidate 전체를 거부한다.

## 검증과 commit 계약

Codex의 선택 테스트는 개발 피드백일 뿐 최종 승인 근거가 아니다.
candidate-verifier는 credential 없이 다음을 고정 순서로 실행한다.

1. `git status --porcelain=v1 -z`와 diff를 파싱해 범위·파일 종류·크기 상한 검사
2. `git diff --check`
3. `uv run --no-sync ruff check agent_orchestration autoresearch tests tools`
4. `uv run --no-sync python -m pytest`

초기 안전 상한은 candidate당 변경 경로 50개, 전체 textual diff 1 MiB, 일반 파일 하나당
10 MiB다. 하나라도 초과하면 일부 변경을 임의로 버리지 않고 candidate 전체를 거부한다.
이 값은 운영 측정치가 아니라 MVP 실행 비용과 검토 가능성을 제한하기 위한 정책값이며,
변경하려면 spec과 verifier 계약을 함께 갱신한다.

검증 명령과 허용 범위는 봉인된 executor image 코드가 소유한다. candidate가 추가하거나
수정한 스크립트를 검증 명령의 정본으로 사용하지 않는다. repository 코드는 GitHub·API·
Codex credential이 없는 verifier에서만 실행한다.

verifier가 승인한 candidate는 Stage 5에 다음 handoff 값을 함께 전달한다.

- 콘텐츠 지문 (`content_fingerprint`): domain separator와 `base_sha`, 정렬된 change
  kind·이전 경로·현재 경로, 각 경로의 missing/regular 상태·mode·size·bytes를 canonical
  SHA-256으로 계산한 64자리 lowercase 값이다.
- 검증 tree 객체 ID (`verified_tree_oid`): verifier snapshot에 최종 commit과 같은
  `git add --all`을 적용한 뒤 `git write-tree`로 얻은 tree OID다.

working tree는 verifier가 만든 snapshot에서 정책과 고정 명령을 실행하며, 원본 candidate의
지문이 snapshot과 verifier 반환 시점에 모두 일치해야 승인한다. Stage 5는 commit 직전에
원본 candidate의 콘텐츠 지문 (`content_fingerprint`)과 staged tree 객체 ID
(`verified_tree_oid`)를 다시 계산한다. 둘 중 하나라도 Stage 4 handoff와 다르면 commit하지
않고 candidate 전체를 거부한다. committed candidate의 `verified_tree_oid`는 검증한 commit의
tree OID와 정확히 일치해야 한다.

성공한 변경은 정확히 한 commit으로 만든다.

- parent: `base_dev_sha`
- author/committer: executor 고정 identity
- message: `exp: issue #<issue_number> candidate`
- branch: 봉인된 `issue_branch`

commit timestamp는 실행 시각이므로 SHA 재생성 결정성을 요구하지 않는다. 멱등성은 이미
push된 유효 candidate를 재생성하지 않고 채택하는 방식으로 보장한다.

## Candidate API와 상태 전이

`Experiment`에 nullable `candidate_sha` 40자리 소문자 컬럼을 추가하고 API 응답에
노출한다. executor 전용 내부 endpoint는 다음 body를 받는다.

```json
{
  "idempotency_key": "executor-candidate:<experiment UUID>",
  "issue_number": 557,
  "issue_branch": "exp/557-example",
  "base_dev_sha": "40자리 소문자 SHA",
  "candidate_sha": "40자리 소문자 SHA"
}
```

endpoint는 transaction 안에서 현재 상태, DB 봉인 좌표, nullable-only candidate,
idempotency key와 request fingerprint를 검증한다. 최초 성공은 `candidate_sha` 저장과
평가 중 (`EVALUATING`) event 생성을 같은 transaction에서 수행한다. 이미 같은 값으로 완료됐다면
기존 결과를 반환하고 다른 candidate 또는 fingerprint는 409로 거부한다.

## 재시도와 충돌

| DB candidate | 원격 tip | 처리 |
| --- | --- | --- |
| 없음 | `base_dev_sha` | Codex 실행 허용 |
| 없음 | parent가 `base_dev_sha`인 executor 형식의 단일 candidate commit | verifier를 다시 수행하고 기존 commit 채택 후 API 보고만 재시도 |
| 같은 SHA | 같은 `candidate_sha` | 멱등 성공 |
| 없음/다름 | 위 조건 외 SHA | 경쟁 writer 충돌로 실패 |

push 직전 원격 tip을 다시 읽어 `base_dev_sha`인지 확인하고 정상 fast-forward push만
사용한다. 경쟁 writer의 non-fast-forward 실패를 충돌로 처리하며 force-push하지 않는다.

Kubernetes Job은 제한된 Pod 재시도만 허용한다. 최종 Failed Job은 launcher reconciler가
TTL 삭제 전에 확인해 Experiment를 오류 (`ERROR`)로 전이하고 정제 사유를 event로 남긴다.
성공 Job인데 candidate 보고가 누락된 경우 같은 입력으로 한 번 복구 실행해 기존 commit
채택 경로를 사용한다.

## 실패 분류

| 실패 구간 | 대표 사유 | 원격 변경 | 최종 상태 |
| --- | --- | --- | --- |
| 이슈 조회 | GitHub 이슈 조회 실패 | 없음 | 오류 (`ERROR`) |
| clone·checkout | 생성 후 ref 부재, HEAD와 원격 tip 불일치 | 없음 | 오류 (`ERROR`) |
| Codex | timeout, 비정상 종료, 변경 없음 | 없음 | 오류 (`ERROR`) |
| verifier | 범위 위반, 테스트·Ruff 실패 | 없음 | 오류 (`ERROR`) |
| push | 원격 tip 경합, 인증·GitHub 실패 | 없음 또는 복구 가능한 candidate | 재시도 후 오류 (`ERROR`) |
| candidate 보고 | API 일시 실패 | candidate commit 존재 가능 | 기존 commit 채택 후 재보고 |

로그에는 실험·이슈·branch·SHA와 정제 사유만 남긴다. token, 환경 덤프, GitHub 응답
body, Codex 원문 stderr는 남기지 않는다.

### Container 실패 관측 계약 (#582)

Kubernetes initContainer는 non-zero 종료 코드만으로는 애플리케이션 실패 원인을 설명하지
못한다. 각 Phase 2 entrypoint는 실행한 stage의 시작과 종료 코드를 남기고, 예외로 종료할
때는 `stage`, 예외 class 이름, 정제된 `reason`을 ERROR 로그로 남긴다.

- executor 도메인 예외의 고정 사유 문자열만 `reason`으로 기록한다.
- 알려지지 않은 stage 인자는 원문을 되풀이하지 않고 `invalid_stage_argument`로 기록한다.
- stage가 예외 없이 non-zero를 반환하면 ERROR `nonzero_exit`와 종료 코드를 기록하고
  정상 종료 로그를 남기지 않는다.
- `OSError`, 외부 라이브러리 예외, 임의 `RuntimeError`·`ValueError`의 원문은 token,
  filesystem 경로 또는 외부 응답을 포함할 수 있으므로 기록하지 않고 `redacted`로
  정규화한다.
- entrypoint 경계의 모든 일반 예외(`Exception`)를 같은 정제 로그와 종료 코드 1로
  수렴시켜 traceback 원문이 container stderr에 노출되지 않게 한다.
- experiment·issue·branch·base SHA는 각각 UUID·양의 정수·실험 branch·40자리 SHA
  형식을 통과한 값만 기록하고, 형식이 다르면 원문 대신 `unknown`을 기록한다.
- 환경 전체, Secret 값·mount 경로, GitHub 응답 body, Codex stdout/stderr는 기록하지
  않는다.
- module entrypoint는 INFO logging을 초기화해 Cloud Logging의 container log만으로
  시작·종료·실패 stage를 식별할 수 있어야 한다.

완료 Job 보존 시간은 launcher 선택 환경 변수 `ORCH_TTL_AFTER_FINISHED_SEC`로 받으며,
미설정 기본값은 기존과 같은 30초다. 장애 smoke 동안만 Infra에서 3600초를 주입할 수
있고, 한 번의 end-to-end 성공 증거를 수집한 뒤 30초로 회수한다. 이 설정은 Pod 권한이나
egress를 넓히지 않는다.

## 이미지와 Infra 계약

Phase 2 executor image는 Python orchestration runtime, Git CLI, uv와 dev/test 의존성,
Node.js와 Codex CLI, prepare/verify/finalize entrypoint를 고정 버전으로 포함한다.
repository 소스는 image에 포함하지 않고 runtime에 봉인 branch를 clone한다.
dev/test 의존성은 `/opt/autoresearch-venv`에 미리 설치하고 candidate 실행 중 sync·install을
허용하지 않는다.

Infra는 다음을 container별로 강제한다.

- branch/clone/push token-minter만 GitHub App private key mount
- codex-worker만 executor 전용 CODEX_HOME Kubernetes Secret mount
- candidate-finalizer만 executor API token mount
- codex-worker와 verifier에는 GitHub/API token volume 미마운트
- `automountServiceAccountToken=false`
- workspace/token volume 상한, non-root UID/GID, seccomp, capability drop
- 승인된 immutable image digest와 고정 command 순서
- GitHub·OpenAI·내부 Experiment API에 필요한 최소 egress

Codex 인증 저장소는 기존 Runner의 writable 세션 상태와 공유하지 않는 executor 전용
Kubernetes Secret 경계로 둔다. 실제 Secret 이름과 resource 수치는 Infra spec/runbook이
소유하고 애플리케이션 테스트는 mount 대상과 container 이름 계약을 고정한다.

### Codex 인증 동시 mount 계약 (#565)

executor의 Codex 인증 원본은 PVC가 아니라 Kubernetes Secret이다. `standard-rwo` PVC는
한 노드에만 attach할 수 있어 동시 실행된 Experiment Pod가 서로 다른 `batch-od` 노드에
배치될 때 다른 Pod의 시작을 막을 수 있다. 작은 read-only `auth.json`은 여러 Pod가 각자
동시에 mount할 수 있는 Secret이 이 실행 계약에 맞는다.

- launcher 설정은 `ORCH_CODEX_HOME_SECRET_NAME`으로 Secret 이름을 받는다.
- Secret은 `auth.json` key 하나를 제공한다. launcher가 Secret volume의 `defaultMode`를
  `0440`으로 지정한다.
- `codex-worker`만 `auth.json` key를 `/var/lib/codex/auth.json`에 read-only `subPath`로
  mount한다. Kubernetes Secret volume의 symlink가 worker의 regular-file 검사를 우회하지
  않도록 파일 단위 mount를 사용하며, 나머지 일곱 컨테이너에는 volumeMount 자체를 만들지
  않는다.
- `codex-worker`는 원본 `auth.json`을 `/tmp` 아래 mode `0700`의 per-run writable
  `CODEX_HOME`으로 복사하고 복사본을 mode `0400`으로 제한한다. 실행 중 인증 상태 변경은
  Pod와 함께 폐기되며 Secret 원본을 갱신하지 않는다.
- Secret이 없거나 `auth.json` key가 없으면 kubelet이 volume mount를 완료하지 못해 Pod는
  `Pending`에 머문다. Job은 `activeDeadlineSeconds` 뒤 `Failed`가 되고,
  launcher는 다음 tick에서 terminal Job을 회수해 Experiment를 `ERROR`로 전환한다. 운영자는
  Pod event의 `FailedMount`와 Job의 `DeadlineExceeded`를 원인 판단 근거로 사용한다.
- `subPath` mount는 실행 중 Secret 갱신을 전파하지 않으므로 Secret 교체는 새 Experiment
  Pod부터 적용한다. 실행 중인 Pod에 인증 교체를 강제하지 않는다.
- 롤아웃은 experiment namespace에 Secret과 `auth.json` key를 먼저 생성한 뒤 새 launcher
  설정과 이미지를 배포한다. 롤백은 이전 launcher 이미지와
  `ORCH_CODEX_HOME_PVC_NAME` 설정을 함께 복원하며, 진행 중 Job이 끝난 뒤 전환해 서로 다른
  인증 계약의 Job이 섞이지 않게 한다.
- 기존 Runner의 `agent-orchestration-codex-home` PVC는 Runner 전용으로 유지하며 executor가
  참조하지 않는다.

### Executor 실행 시간 예산 (#567)

`activeDeadlineSeconds`는 Codex 한 단계가 아니라 token 발급, branch 생성, clone, Codex,
Ruff·전체 pytest, commit·push, Candidate API 보고를 포함한 8-container Job 전체에 적용된다.
기존 300초 고정값은 Codex 상한 120초와 전체 pytest 약 138초만 합쳐도 258초여서 다른 여섯
단계에 42초밖에 남기지 못하므로 운영 smoke의 완주 상한으로 사용할 수 없다.

- launcher는 `ORCH_ACTIVE_DEADLINE_SEC`와 `ORCH_CODEX_TIMEOUT_SEC`를 모두 필수 양의 정수로
  읽고 Codex 상한이 Job 전체 상한 이상이면 기동 전에 fail-closed한다.
- MVP 운영값은 admission 허용 상한 안에서 Job 전체 `3600`초, Codex `1800`초로 고정한다.
  남은 1800초는 token 발급·branch·clone·verifier·finalizer와 변동 여유 시간이다.
- launcher가 만드는 Phase 1 branch Job과 Phase 2 executor Job은 동일한
  `ORCH_ACTIVE_DEADLINE_SEC` 값을 사용한다. 현재 운영 launcher는 Phase 2 Job을 생성한다.
- 실제 단계별 소요 시간은 smoke에서 관측하며, 추정값을 성공 지표로 기록하지 않는다.

## 검증 전략

### 단위·계약 테스트

- Issue Form 형태와 무관한 GitHub 이슈 원문 전달
- 지정 branch clone·checkout과 원격 tip 확인
- 허용·금지 경로와 symlink/submodule 거부
- Codex 환경 allowlist와 credential 부재
- verifier 명령·결과 파싱
- commit parent/message/branch 고정과 원격 tip 재시도 표
- candidate API idempotency와 409 충돌
- image의 Git·uv·Node·Codex와 고정 entrypoint
- container별 Secret·volume 분리, non-root, ServiceAccount token 미마운트

### 통합 테스트

- 임시 bare Git server와 fake issue/API/Codex로
  clone → 수정 → 검증 → commit → push → candidate 보고를 관통한다.
- 금지 파일 또는 테스트 실패 시 원격 ref가 그대로인지 확인한다.
- push 성공 뒤 API 실패를 주입하고 재시도가 새 commit 없이 기존 SHA를 보고하는지
  확인한다.

### 운영 smoke

새 Experiment 하나에서 다음을 대조한다.

- DB `base_dev_sha` == candidate commit parent
- DB `issue_branch` == push된 branch
- DB `candidate_sha` == GitHub 원격 tip
- Experiment status == 평가 중 (`EVALUATING`)
- candidate commit은 정확히 한 개
- Codex/verifier 환경·mount·로그에 GitHub/API credential 없음
- `main`, `dev`, 다른 `exp/*` ref 변화 없음

## 배포 순서와 소유권

1. Autoresearch spec·plan 승인
2. migration/API/executor/launcher 구현과 CI
3. immutable executor·launcher·API image 게시
4. Autoresearch-infra companion 이슈·PR에서 admission, Secret, NetworkPolicy 반영
5. 별도 승인 후 Infra apply
6. 새 Experiment 운영 smoke
7. Phase 2 성공 증거 확인 후 후속 평가 파이프라인 연결

Airflow는 candidate SHA 이후 학습·평가 소비를 소유하며 이 spec 구현에는 관여하지 않는다.
