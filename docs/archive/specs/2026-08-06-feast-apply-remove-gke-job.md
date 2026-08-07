# `feast-apply.yml`에서 GKE Job 제거, 셀프 호스티드 러너 직접 실행 (#561)

## 배경

`feast-apply.yml`은 #346에서 GKE Job으로 우회하도록 설계됐다. 이유는 GHA
hosted runner(`ubuntu-latest`)가 VPC 밖이라 private Redis(PSC)에 닿지 못해서다
— `full_scan_for_deletion: true`가 요구하는 online store 삭제 스캔은 Redis
접속이 필수다. 그래서 워크플로우는 Job을 만들고 결과만 판정하고, 실제 apply는
`deploy/feast/apply-job.yaml`이 정의하는 GKE Job Pod(`Dockerfile.feast` 이미지,
`ENTRYPOINT`가 GCS 코드 아카이브를 부트스트랩)에서 실행된다.

인프라 저장소(`SKYAHO/Autoresearch-infra`)가 `feast-apply-dev`/`feast-apply-prod`
전용 셀프 호스티드 러너 스케일셋(ARC)을 VPC 안에 배포했고(#541), K8s API
egress 사고(#557/#558)를 수정한 뒤 GCS·Redis PSC 도달성을 실제로
검증했다(#556/#555 — `feast-apply-runner-probe-caller.yml`). 이제 GKE Job으로
우회할 이유가 없다: 러너 자체가 VPC 안에서 돈다.

## 결정

`feast-apply.yml`을 `runs-on: feast-apply-${environment}`(ARC 스케일셋 직접
지정)로 바꾸고, GKE Job을 만드는 대신 러너 위에서 `feast apply`를 **직접**
실행한다. Job 전용 코드 조달 경로(GCS 코드 아카이브 부트스트랩)는 러너의
`actions/checkout`이 대체하므로 함께 제거된다.

### 왜 이 방향인가

- Job 방식은 "코드를 실행 가능한 위치로 옮기는 계층"(GHA→GKE Job)이 이중이다
  — 셀프 호스티드 러너 자체가 이미 그 실행 위치이므로 계층 하나가
  통째로 사라진다.
- 이중 계층이 낳은 부수 복잡도(코드 아카이브 대기, 부트스트랩 마커 검증,
  Job 생성/삭제/대기/로그수집/describe 5스텝)가 함께 없어진다.
- Job 방식 고유의 실패 모드(#548 — 잘못된 GitHub Environment로 뜬 잡이
  자격증명은 통과하고 한참 뒤에야 실패)는 이미 `environment:` 표현식
  수정으로 해소돼 있고, 이 변경과 독립적이다.

## 바뀌는 것

### 제거

| 대상 | 이유 |
| --- | --- |
| `Get GKE credentials` 스텝 | kubectl로 GKE API를 부를 필요가 없다 |
| `Render Job manifest` 스텝 | Job manifest 자체가 없다 |
| `Recreate Job` 스텝 | 〃 |
| `Wait for Job result` 스텝 | 러너 스텝은 GHA가 직접 기다린다(잡 자체가 곧 실행) |
| `Collect Job logs` 스텝 | 러너 스텝 stdout이 곧 로그다 |
| `Describe Job on failure` 스텝 | describe 대상 Job이 없다 |
| `Guard against Job failure` 스텝 | 다음 스텝(`feast apply` 실행)의 종료 코드가 그 역할을 대신한다 |
| `Wait for the code archive of this commit` 스텝 | 러너가 `actions/checkout`으로 이미 정확한 커밋을 갖는다 — GCS 코드 아카이브를 기다릴 필요가 없다 |
| `Guard against wrong code version (bootstrap marker)` 스텝 | 위와 같은 이유로 "실행된 코드 버전"을 별도로 단언할 필요가 없다(체크아웃 자체가 그 보증이다) |

### 유지

- `Checkout repository`, `dev 환경 좌표 확인`(`resolve-dev-environment`),
  `Authenticate to GCP with Workload Identity Federation`,
  `Set up Cloud SDK`, `Validate required configuration`,
  `Verify the environment credentials before waiting`(이름은 "waiting"이지만
  더 이상 대기 스텝이 없으므로 이름만 정리 — 내용은 유지).
- `Record registry generation before apply` / `Guard against silent apply
  failure (registry generation)` — GCS 객체 generation 비교라 Job 방식과
  무관하다.
- `Guard against silent apply failure (log pattern)` — `apply.log`를 계속
  만들어야 하므로, 직접 실행 스텝에서 stdout을 `tee apply.log`로 남긴다.
- `Set up uv` / `Install feast group for registry verification` /
  `Verify the applied registry loads in the consumer import path` — 다만
  feast 설치 스텝은 apply 실행보다 **먼저** 필요해지므로 위치를 옮긴다(아래
  참고).
- `deploy/feast/apply-job.yaml`, `Dockerfile.feast`, feast 이미지 빌드
  파이프라인, 인프라 저장소의 `feast_apply.tf`(#346) — 전부 롤백 여유로
  그대로 둔다. 이번 변경으로 이 자산들을 참조하는 곳이 없어지지만
  삭제하지 않는다(별도 issue).

### 새로 추가

`feast apply` 직접 실행 스텝 1개가 `Render Job manifest`~
`Guard against Job failure`(옛 5스텝)를 대체한다. `deploy/feast/apply-job.yaml`
컨테이너 `args`가 하던 일을 그대로 옮긴다:

```bash
python scripts/fetch_redis_ca.py "$REDIS_TLS_CA_PATH"
cd feature_repo
feast --log-level debug apply 2>&1 | tee ../apply.log
```

환경 변수는 옛 Job `env:` 블록과 **완전히 동일한 이름**을 쓴다 — Feast는
`feature_store.yaml`의 `${VAR}` 자리를 로드 시점에 **프로세스 환경변수에서**
치환하므로(envsubst 같은 별도 렌더 단계가 없다), Job에 주입하던 이름을
step/job `env:`로 그대로 옮기면 된다:
`GCP_PROJECT_ID`, `GOOGLE_CLOUD_PROJECT`, `BQ_DATASET`, `GCS_REGISTRY_PATH`,
`GCS_STAGING_LOCATION`, `REDIS_HOST`, `REDIS_PORT`, `REDIS_CA_SECRET_ID`,
`REDIS_TLS_CA_PATH`, `AUTORESEARCH_ENV`, `FEAST_ONLINE_FULL_SCAN_FOR_DELETION`.

`CODE_ARTIFACTS_BUCKET`/`CODE_ARCHIVE_SHA`는 GCS 부트스트랩 전용이라 제거한다.

`PYTHONPATH`는 옛 컨테이너에서 `/app`(리포 루트, `feature_repo`의 부모)이었다
— 러너에서는 체크아웃 루트(`${{ github.workspace }}`)가 그 역할을 한다.
`feature_store.yaml`의 `online_store.type: feature_repo.redis_iam.IAMRedisOnlineStore`
임포트가 이 값에 의존하므로 빠뜨리면 `ModuleNotFoundError`로 apply가 즉시
죽는다.

`runs-on`은 `environment:`/`AUTORESEARCH_ENV`와 **같은 식**을 그대로 써야
한다 — `runs-on`은 `env:` 컨텍스트를 읽지 못하므로(잡 시작 전에 평가) job
`env.AUTORESEARCH_ENV`를 참조할 수 없다:

```yaml
runs-on: feast-apply-${{ github.event_name == 'workflow_dispatch' && inputs.environment || (github.ref_name == 'main' && 'prod' || 'dev') }}
```

값은 인프라 저장소 `deploy/actions-runner-scale-set-feast-{dev,prod}/values.yaml`의
`runnerScaleSetName`(`feast-apply-dev`/`feast-apply-prod`)과 정확히 일치해야
매칭된다(ARC는 `runs-on`에 스케일셋 이름을 그대로 쓴다 — 참고:
`feast-apply-runner-probe.yml:81`, `actions-runner-poc.yml:21`).

### feast 설치 시점 이동

현재 `uv sync --frozen --no-dev --group feast`는 apply "이후" registry 검증
스텝 앞에서만 실행된다(hosted runner라 `feast apply` 자체는 컨테이너 안에서
돌았으므로 러너에 feast를 깔 필요가 없었다). 직접 실행 방식에서는 apply
자체가 러너 프로세스이므로, `Set up uv`/`uv sync --group feast`를 **apply
스텝보다 앞으로** 옮기고, 뒤쪽 검증 스텝은 같은 설치를 재사용한다(중복
설치 스텝 제거).

## 위험 / 미해결

- **`resolve-dev-environment` composite action이 `ruby`를 전제한다**
  (`environment_catalog.rb` 실행). GitHub hosted runner(`ubuntu-latest`)는
  ruby가 기본 포함이지만, ARC 셀프 호스티드 러너 이미지에 ruby가 있는지는
  확인되지 않았다. `google-github-actions/setup-gcloud`/`astral-sh/setup-uv`는
  자체 바이너리를 내려받아 설치하므로 베이스 이미지와 무관하게 동작하지만,
  `resolve-dev-environment`는 그런 self-install 스텝이 없다 — 없으면
  `ruby: command not found`로 즉시 실패한다. **구현 착수 전에 러너에서
  `ruby --version`을 먼저 확인**하고, 없으면 (a) 러너 이미지에 ruby를
  추가하거나(인프라 저장소 범위) (b) 이 composite action의 ruby 의존을
  제거하는 대안(#541/#548과 별개 이슈)을 먼저 정리한다.
- 네트워크 egress 자체(GitHub 443, K8s API, Redis PSC prod만)는 #557/#558로
  검증된 상태라 재확인 대상이 아니다. `setup-gcloud`가 받는 gcloud SDK
  다운로드도 baseline 정책의 `0.0.0.0/0:443`(RFC1918 제외) 규칙에 이미
  포함된다 — 별도 NetworkPolicy 변경 불필요.
- `feast-apply-dev`/`feast-apply-prod` 러너는 동시에 1개 job만 처리 가능한
  용량으로 설계됐는지(#264 `max_pods`) 확인한다 — push 트리거가 겹치면
  큐잉되는 게 맞는지, 아니면 병렬 실행 시 `apply.log` 등 로컬 파일 경합이
  있는지는 ARC ephemeral runner Pod가 job마다 새로 뜨므로 원래 경합이 없다
  (각 job = 별도 Pod). 확인만 하고 코드 변경은 없다.

## 범위 제외

- `feast_apply.tf`(인프라 #346), `deploy/feast/apply-job.yaml`,
  `Dockerfile.feast`, feast 이미지 빌드 파이프라인 삭제 — 롤백 여유,
  별도 issue.
- `resolve-dev-environment`의 ruby 의존 제거 자체 — 위 위험 절에서 확인만
  하고, 실제로 없다고 확인되면 별도 이슈로 분리한다(이 변경의 blocking
  issue가 아니라면).
