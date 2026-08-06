# `feast-apply.yml` GKE Job 제거 — 구현 계획 (#561)

정본 계약: `docs/specs/2026-08-06-feast-apply-remove-gke-job.md`

## 범위 상태

| Task | 상태 |
| --- | --- |
| T1. `runs-on` + 환경 표현식 전환 | 미착수 |
| T2. GCS 부트스트랩 의존 스텝 제거 | 미착수 |
| T3. feast 설치 스텝 재배치 | 미착수 |
| T4. 직접 실행 스텝 추가(CA 조달 + apply) | 미착수 |
| T5. 3개 침묵 실패 가드 재배선 | 미착수 |
| T6. 문서 갱신 (`docs/TERRAFORM_DEV.md` 등) | 미착수 |
| T7. dev/prod 각 1회 실행 검증 | 미착수(구현 PR에서 수행, 이 plan PR 범위 아님) |

## 미해결 — 착수 전/중 확정

| 항목 | 막는 Task | 확인 방법 |
| --- | --- | --- |
| ARC `feast-apply-{dev,prod}` 러너 이미지에 `ruby`가 있는가 | T1(모든 스텝이 `resolve-dev-environment`를 거친다) | `workflow_dispatch`로 임시 진단 잡을 해당 러너에서 1회 실행해 `ruby --version` 확인, 또는 인프라 저장소 러너 이미지/Dockerfile 확인 |
| `feast-apply-{dev,prod}` 스케일셋이 동시 1 job만 처리하는가(#264 `max_pods`) | 없음(정보성, 코드 변경 트리거 아님) | 인프라 저장소 `deploy/actions-runner-scale-set-feast-{dev,prod}/values.yaml`의 `maxRunners` 확인 |

ruby 미확인 항목이 없다고 판명되면 T1은 그대로 진행. 있다고 판명되면(러너에
ruby 없음) T1 착수 전에 별도 이슈로 (a) 러너 이미지에 ruby 추가(인프라
저장소) 또는 (b) `resolve-dev-environment`의 ruby 의존 제거 중 하나를
선결해야 한다 — 이 plan은 그 선결 이슈의 완료를 전제로 한다.

## 범위 제외 (다루지 않는 것과 사유)

- `deploy/feast/apply-job.yaml`, `Dockerfile.feast`, feast 이미지 빌드
  파이프라인, 인프라 저장소 `feast_apply.tf` 삭제 — 롤백 여유를 위해
  스펙에서 명시적으로 보존을 결정했다. 별도 issue.
- `resolve-dev-environment`의 ruby 의존 자체를 없애는 리팩터 — 위 미해결
  표의 확인 결과 실제로 없다고 판명될 때만, 별도 이슈로 분리.
- feast-apply 러너 스케일셋 자체의 용량/오토스케일 튜닝 — 인프라 저장소
  범위, 이 저장소에서 건드리지 않는다.
- `feast-apply.yml` 외 다른 워크플로우(`code-archive.yml` 등)의 GCS 코드
  아카이브 파이프라인 자체 — 다른 소비자(`Dockerfile.feast`가 여전히 이
  아카이브를 참조)가 있어 그대로 둔다.

## 파일별 책임

| 파일 | 이번 변경에서 하는 일 |
| --- | --- |
| `.github/workflows/feast-apply.yml` | T1~T6 전부가 이 파일 안에서 일어난다 — 유일한 수정 대상 |
| `docs/TERRAFORM_DEV.md` (또는 그 안의 관련 절) | T6: "hosted runner가 VPC 밖" 서술 갱신 |
| `docs/plans/2026-08-06-feast-apply-remove-gke-job.md`(본 문서) | 이번 PR 자체의 산출물 |
| `docs/specs/2026-08-06-feast-apply-remove-gke-job.md` | 이번 PR 자체의 산출물(정본 계약) |

### 건드리지 않는 파일과 이유

- `deploy/feast/apply-job.yaml` — 범위 제외 항목, 보존 결정.
- `Dockerfile.feast` — 〃.
- `feature_repo/feature_store.yaml` — `${VAR}` 치환 메커니즘은 이미 Feast
  네이티브 동작이라 파일 수정 없이 그대로 재사용된다.
- `feature_repo/redis_iam.py`, `scripts/fetch_redis_ca.py` — 호출 방식(위치)만
  워크플로우 쪽에서 바뀌고, 스크립트 자체 인터페이스는 그대로다.
- 인프라 저장소 전체 — 이번 변경은 앱 저장소의 워크플로우 파일 1개로
  닫힌다. 러너 스케일셋 자체(#541)는 이미 존재하므로 인프라 쪽 변경이
  필요 없다(단, 위 미해결 표의 ruby 항목이 "없음"으로 확인되면 인프라
  저장소에 별도 이슈가 파생될 수 있다).

## 회귀 판정 기준 (baseline)

이 변경은 워크플로우 YAML 1개만 수정하며 애플리케이션 코드/테스트에
영향을 주지 않는다. 따라서 pytest 스냅샷 baseline은 이 plan에서는
불필요 — 대신 워크플로우 자체의 실행 성공/실패로 판정한다.

- 판정 대상은 **dev/prod 각 1회 실제 실행**(#561 완료 조건과 동일)이며,
  이 plan-doc PR 자체에는 포함하지 않는다(정본 계약이 명시한 대로 구현
  PR에서 수행).
- 로컬에서 가능한 최소 검증은 YAML 문법과 GHA 스키마 유효성뿐이다(아래
  전체 검증 참고).

## Task 별 체크리스트

### T1. `runs-on` + 환경 표현식 전환

- [ ] `runs-on: ubuntu-latest` →
      `runs-on: feast-apply-${{ github.event_name == 'workflow_dispatch' && inputs.environment || (github.ref_name == 'main' && 'prod' || 'dev') }}`
      (스칼라 문자열, 배열 아님 — ARC 스케일셋 이름과 정확히 일치해야
      매칭된다).
- [ ] 인프라 저장소 `deploy/actions-runner-scale-set-feast-{dev,prod}/values.yaml`의
      `runnerScaleSetName`이 `feast-apply-dev`/`feast-apply-prod`인지 재확인
      (변경 없음, 확인만).

**RED** (이 스텝이 잘못됐을 때 나타나는 구체적 실패):
- 배열 문법을 쓰면 잡이 "Waiting for a runner..."로 무한 대기(hosted
  runner든 self-hosted든 매칭되는 라벨 집합이 없어짐) → 타임아웃으로
  실패, 에러 메시지가 문법 문제를 직접 가리키지 않아 원인 파악이
  느리다. 반드시 스칼라 문자열로 확인.
- `runs-on` 표현식이 job `env:`를 참조하면 evaluation 단계에서
  "Unrecognized named-value" 에러로 즉시 실패(문법 에러이므로 다른
  스텝과 구별 쉬움).

### T2. GCS 부트스트랩 의존 스텝 제거

- [ ] `Wait for the code archive of this commit` 스텝 삭제.
- [ ] `Guard against wrong code version (bootstrap marker)` 스텝 삭제.
- [ ] `Get GKE credentials`, `Render Job manifest`, `Recreate Job`,
      `Wait for Job result`, `Collect Job logs`, `Describe Job on failure`,
      `Guard against Job failure` 7개 스텝 삭제.

**RED**:
- 삭제 후 다음 스텝(T4의 직접 실행)이 `feature_repo/redis_iam.py`를
  import하지 못하면 GKE Job 없이도 여전히 실패해야 정상(코드는 checkout이
  이미 갖고 있으므로 이 실패 모드 자체가 나타나지 않는 것이 성공 기준).
- 이 7개 스텝이 참조하던 GHA Job output(`steps.wait-for-job.outputs.*`
  등)이 뒤 스텝에 남아있으면 "context access might be invalid" 경고/에러가
  뜬다 — 전체 grep으로 잔여 참조가 없는지 확인.

### T3. feast 설치 스텝 재배치

- [ ] `Set up uv` + `uv sync --frozen --no-dev --group feast`를 T4(직접
      실행) 스텝보다 앞으로 이동.
- [ ] 기존 "registry 검증용" 위치의 동일 설치 스텝은 제거(중복 설치
      없이 앞으로 옮긴 설치를 재사용).

**RED**:
- 재배치 후 `feast` 커맨드가 apply 스텝에서 `command not found`로
  실패하면 이동이 안 된 것.
- 뒤쪽 검증 스텝(`Verify the applied registry loads in the consumer
  import path`)이 재사용 가능한지 — 별도 `uv sync` 없이도 import가
  성공해야 한다(같은 venv를 같은 job 안에서 재사용하므로 성공 기준).

### T4. 직접 실행 스텝 추가 (CA 조달 + apply)

- [ ] 새 스텝 `Fetch Redis CA and run feast apply` 추가:
  ```yaml
  - name: Fetch Redis CA and run feast apply
    working-directory: ${{ github.workspace }}
    env:
      PYTHONPATH: ${{ github.workspace }}
      GCP_PROJECT_ID: ...
      GOOGLE_CLOUD_PROJECT: ...
      BQ_DATASET: ...
      GCS_REGISTRY_PATH: ...
      GCS_STAGING_LOCATION: ...
      REDIS_HOST: ...
      REDIS_PORT: ...
      REDIS_CA_SECRET_ID: ...
      REDIS_TLS_CA_PATH: /tmp/redis-ca.pem
      AUTORESEARCH_ENV: ...
      FEAST_ONLINE_FULL_SCAN_FOR_DELETION: ...
    run: |
      set -euo pipefail
      python scripts/fetch_redis_ca.py "$REDIS_TLS_CA_PATH"
      cd feature_repo
      feast --log-level debug apply 2>&1 | tee ../apply.log
  ```
  (기존 워크플로우에 이미 정의된 이 값들의 소스 — `env:`/`vars:`/이전
  스텝 output — 는 옛 `Render Job manifest` 스텝의 `envsubst` 입력과
  1:1 대응하므로, 그 스텝의 변수 소스를 그대로 옮긴다.)
- [ ] `CODE_ARTIFACTS_BUCKET`/`CODE_ARCHIVE_SHA`는 옮기지 않는다(GCS
      부트스트랩 전용, 더 이상 필요 없음).

**RED**:
- `PYTHONPATH` 누락 시 `ModuleNotFoundError: No module named
  'feature_repo'`로 apply가 즉시 죽는다 — 이 특정 에러 메시지가 나오면
  누락 신호.
- `REDIS_TLS_CA_PATH` 값이 `fetch_redis_ca.py`의 출력 경로와
  `feature_store.yaml`이 참조하는 경로가 다르면 TLS 핸드셰이크 실패로
  apply가 죽는다 — 두 경로 문자열이 스텝 안에서 정확히 같은지 diff.
- `apply.log`가 생성되지 않으면 T5의 로그 패턴 가드가 파일 없음으로
  실패 — `tee`가 빠지지 않았는지 확인.

### T5. 3개 침묵 실패 가드 재배선

- [ ] `Record registry generation before apply` — 위치만 T4 스텝
      바로 앞으로 유지(로직 변경 없음).
- [ ] `Guard against silent apply failure (log pattern)` — `apply.log`
      경로가 T4에서 만든 파일과 일치하는지만 확인, 로직 변경 없음.
- [ ] `Guard against silent apply failure (registry generation)` — 로직
      변경 없음.
- [ ] `Verify the applied registry loads in the consumer import path` —
      T3에서 재배치한 feast 설치를 재사용, 로직 변경 없음.

**RED**:
- 세 가드 중 하나라도 옛 Job 경로의 output 변수(`steps.wait-for-job...`)를
  참조하고 있었다면 여기서 미해결 참조로 걸린다 — grep으로 전수 확인.

### T6. 문서 갱신

- [ ] `docs/TERRAFORM_DEV.md`(또는 `feast-apply.yml` 상단 주석에 여전히
      남아있는 "hosted runner는 VPC 밖" 서술)을 "셀프 호스티드 러너가
      VPC 안에서 직접 실행" 서술로 교체.
- [ ] `feature_repo/feature_store.yaml`의 `online_store.full_scan_for_deletion`
      주석("apply 는 VPC 안의 GKE Job 에서 실행한다 (#346,
      deploy/feast/apply-job.yaml)")도 갱신 대상인지 확인 — GKE Job
      경로가 여전히 파일로는 존재하므로(범위 제외), 주석은 "현재
      기본 경로는 셀프 호스티드 러너 직접 실행(#561), GKE Job은
      롤백용으로 보존"으로 수정.

**RED**: 없음(문서 변경은 실행 실패로 드러나지 않음) — 리뷰로만 검증.

## 전체 검증

```bash
cd /Users/buzz/Desktop/Autoresearch
git diff --check
# actionlint가 로컬에 있으면
actionlint .github/workflows/feast-apply.yml
```

이 plan-doc PR 자체는 `.github/workflows/feast-apply.yml`을 아직 수정하지
않으므로(문서만 추가) 위 명령은 **구현 PR** 단계에서 실행한다. plan-doc
PR에서는 신규 마크다운 2개 파일의 `git diff --check`만으로 충분하다.

구현 PR 완료 조건(정본 계약과 동일, 재기재):
- dev push 또는 `workflow_dispatch`로 1회 성공.
- prod push 또는 `workflow_dispatch`로 1회 성공.
- 기존 GKE Job 경로 파일은 삭제하지 않고 남아있음을 `git status`로 확인.
