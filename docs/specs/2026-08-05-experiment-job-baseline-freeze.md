# 실험 Job 기준 커밋 고정 계약

## 목적

이 문서는 #546 Phase 1에서 CronJob launcher가 생성한 executor Pod가
`exp/<issue>-<slug>` 브랜치를 만들 때, 대기열 중간의 코드 변경 때문에 실험마다
서로 다른 기준 코드를 사용하지 않도록 하는 불변 계약이다.

Pod가 시작할 때마다 최신 ref를 읽어 브랜치를 만들면, 동시 실행 상한 때문에
대기한 실험은 먼저 실행된 실험과 다른 코드에서 시작한다. 이 문서는 기준 커밋을
**실험을 수락해 대기열에 넣는 시점**에 고정하고, Pod는 저장된 SHA만 사용하게 한다.

## 용어와 기준 ref

- `base_dev_sha`: 실험의 변경 전 기준 코드인 40자리 Git commit SHA이다.
- 기준 ref는 현재 Auto Research 계보 계약과 같은 `dev`다. `main`은 승격 대상이며
  executor의 branch 생성 기준으로 읽지 않는다.
- `issue_branch`: `exp/<issue>-<slug>` 형식의 실험 branch 이름이다.
- 기준선 고정: `base_dev_sha`를 확정한 뒤 `dev` 또는 `main`이 변경되어도 그 값이
  바뀌지 않는 성질이다.

`main` 또는 `dev`의 이후 변경은 이미 고정된 SHA에서 만든 branch의 부모를 바꾸지
않는다. 따라서 대기 중인 Pod가 늦게 시작해도 같은 기준선에서 branch를 만든다.

## 기준선 수명주기

```text
Experiment 이슈 발행 요청 수락
  → read-only GitHub App installation token으로 dev ref를 한 번 읽음
  → issue_body, issue_title, base_dev_sha를 발행 전에 먼저 저장
  → 이슈 발행 후 issue_number, issue_branch 저장
  → launcher가 experiment_id, issue_number, issue_branch, base_dev_sha를 executor Job에 전달
  → Pod가 저장된 base_dev_sha에서만 exp branch 생성
```

1. launcher와 executor는 branch 생성 시점에 `heads/dev` 또는 `heads/main`의 최신
   tip을 조회해 기준으로 사용해서는 안 된다.
2. dispatch 대상 Experiment에는 유효한 `issue_number`, `issue_branch`,
   `base_dev_sha`가 모두 있어야 한다. 하나라도 없거나 SHA 형식이 아니면 Job을 만들지
   않고 fail-closed 처리한다.
3. executor Pod는 전달받은 `base_dev_sha` object를 조회한 뒤 그 SHA를 정확히 가리키는
   `refs/heads/<issue_branch>`를 생성한다. ref 생성 성공 뒤에 다른 기준 SHA로 rebase,
   reset 또는 force-push하지 않는다.
4. 재시도에서 branch가 이미 있으면 Phase 1은 tip이 정확히 `base_dev_sha`와 같은 경우만
   멱등 성공으로 인정한다. 다른 tip은 사람이 만든 변경 또는 이전 실행의 부작용일 수
   있으므로 새 ref를 만들거나 덮어쓰지 않고 실패한다.

`base_dev_sha`는 이슈 발행보다 먼저 commit한다. 이슈는 열렸지만 API 응답이나 DB 후속
쓰기가 실패한 경우에도 재호출이 최신 `dev`를 다시 읽어 기준선을 바꾸면 안 되기 때문이다.
이미 저장된 `base_dev_sha`가 있으면 재발행은 그 값을 그대로 사용한다.

현재 `Experiment`에는 `base_dev_sha` 컬럼이 없으므로, 구현 시 migration과 API 응답·Job
입력 계약을 함께 추가해야 한다. 기존 `issue_branch`는 이슈 발행 직후 계산해 저장한
예상 branch 좌표일 뿐, 실제 Git ref의 존재를 증명하지 않는다.

## 개별 기준선과 비교 집합 기준선

두 경우를 혼동하지 않는다.

| 입력 의도 | `base_dev_sha`를 읽는 횟수 | 저장 규칙 | 비교 가능성 |
| --- | --- | --- | --- |
| 서로 독립적인 가설 | 각 Experiment 수락 시 1회 | 각 행에 당시 SHA를 저장 | 서로 다른 SHA일 수 있으므로 코드 변경 효과를 직접 비교하지 않는다 |
| 같은 조건에서 비교할 가설 100개 | 비교 집합 수락 시 전체에 대해 1회 | 모든 행에 동일 SHA와 같은 `baseline_cohort_id`를 저장 | 기준 코드가 같으므로 이후 데이터·seed 계약까지 같다면 공정 비교의 전제가 된다 |

"동시에 100개를 입력했다"는 사실만으로 공통 기준선이 생기지 않는다. 공통 비교를
의도한 호출자는 명시적인 비교 집합으로 등록해야 하며, 서버는 한 번 읽은 SHA를 그 집합의
모든 Experiment에 기록해야 한다. Phase 1은 개별 Experiment의 `base_dev_sha` 저장과
전달을 구현한다. `baseline_cohort_id`를 받는 batch 등록 API는 이후 범위다.

## Pod 내부 branch 생성 경계

exp branch의 생성 주체는 executor Pod 하나다. `auto-experiment` label을 받는 GitHub
Actions workflow는 더 이상 ref를 생성하지 않는다. 그렇지 않으면 Actions와 Pod가 같은
branch를 동시에 만들 수 있다.

Pod가 GitHub에 branch를 만들려면 GitHub Contents write 권한이 필요하다. 이 권한은
launcher의 Kubernetes Job 생성 권한과 분리한다. branch 생성 이후의 Codex 실행, 코드
변경, 검증, candidate SHA 생성·push는 이 Phase 1의 범위가 아니다.

launcher는 Experiment를 `CREATED`에서 `RUNNING`으로 선점할 때 결정론적 Job 이름을 먼저
저장하고, Kubernetes에서 그 Job이 존재함을 확인한 시각을 별도 기록한다. 선점과 Job 생성
사이에서 launcher가 종료되면 다음 tick은 생성 확인 시각이 없는 행만 같은 이름과 봉인된
좌표로 재개한다. 생성 확인 시각이 있는 행은 TTL로 Job이 삭제되어도 다시 만들지 않는다.
Phase 1은 Job 완료·실패를 새 DB status로 회수하지 않으므로 branch 생성 성공 뒤에도
Experiment는 `RUNNING`에 남는다. 상태 reconciler는 후속 Phase의 별도 계약이다.

Phase 1에서 `CREATED → RUNNING` 전이의 운영 소유자는 launcher다. 기존 범용 status/event
API는 호환성을 위해 이 전이를 아직 표현할 수 있지만, launcher보다 먼저 호출하면 Job 이름
없는 `RUNNING` 행이 되어 두 claim query에서 제외된다. 배포된 UI·자동화는 이 전이를 직접
호출하지 않으며, 외부 실행 주체에 범용 상태 쓰기를 개방하기 전 launcher 전용 전이 강제
또는 정체 행 회수 계약을 후속 Phase에서 정해야 한다.

Job 생성 확인 뒤 executor가 실패하면 Phase 1에는 자동 재시도나 DB status reconciler가
없다. executor와 token-minter는 시크릿을 제외한 예외 종류·정제 사유·HTTP 상태를 Pod
로그에 남기지만, 기본 TTL 30초 뒤 Job/Pod와 함께 사라진다. 영속 실패 이력과 재시도 정책은
상태 reconciler와 함께 후속 Phase에서 정의한다.

## GitHub App Installation Token 계약

GitHub 자격 증명은 개인 PAT가 아니라 GitHub App installation token을 사용한다. 기준 SHA
조회와 branch 생성은 권한 수명이 다르므로 App도 분리한다.

| App 역할 | 설치 범위 | 최대 repository permission | private key 보유 주체 | 사용 시점 |
| --- | --- | --- | --- | --- |
| baseline reader | `SKYAHO/Autoresearch` 한 저장소 | Contents read | Agent Orchestration API Pod | 이슈 발행 전 `heads/dev` 1회 조회 |
| branch writer | `SKYAHO/Autoresearch` 한 저장소 | Contents write | executor의 token-minter initContainer | executor Pod 시작 직후 ref 조회·생성 |

baseline reader의 App은 read-only라 API가 손상되어도 ref를 만들거나 수정할 수 없다. API는
installation token을 발급해 `heads/dev`를 읽고, 검증한 40자리 SHA를 이슈 발행보다 먼저
DB에 저장한다. 기존 `issues: write` 전용 `ORCH_GITHUB_TOKEN`은 이슈 발행에만 유지하며
Contents 권한으로 넓히지 않는다.

branch writer의 private key는 executor 애플리케이션 컨테이너에 전달하지 않는다.

```text
executor Pod 시작
  → token-minter initContainer만 GitHub App private key를 read-only mount
  → installation token 발급
  → medium: Memory emptyDir에 mode 0400 파일로 저장
  → initContainer 종료
  → executor는 token 파일만 읽어 GitHub Git refs REST API 호출
  → Pod 종료와 함께 token 파일 삭제, token은 최대 1시간 뒤 만료
```

토큰은 환경 변수, command argument, Git remote URL, `.git/config`, stdout/stderr에 넣지
않는다. Phase 1은 checkout이나 push를 하지 않고 Git refs REST API만 사용한다. 기존 ref가
없으면 `POST /repos/{owner}/{repo}/git/refs`로 생성하고, 이미 있으면 tip 조회 결과가
`base_dev_sha`와 정확히 같은 경우만 멱등 성공으로 인정한다. 401 또는 토큰 만료는 새
토큰을 같은 Pod에서 반복 발급하지 않고 Job 실패로 처리하며, 새 Job 시도가 새 토큰을
발급한다.

GitHub installation token은 저장소 단위 Contents 권한이며 `exp/*` ref만 쓰도록 제한할 수
없다. 따라서 `main`·`dev` ruleset은 계속 마지막 방어선이다. Phase 1의 executor 이미지는
검증된 branch bootstrap 코드만 포함하고 Codex 또는 사용자 코드를 실행하지 않는다.
Codex workspace-write가 들어오는 후속 단계는 branch writer private key와 token을 같은
Pod에 두지 않는다. 그 단계에 들어가기 전에 token broker 또는 동등한 별도 쓰기 경계를
설계·승인해야 한다.

현재 dev GKE는 Calico NetworkPolicy를 사용하므로 GKE Dataplane V2 전용
`FQDNNetworkPolicy`를 바로 적용할 수 없다. Phase 1에서는 branch bootstrap label을 가진
신뢰된 Pod에만 TCP 443 공용 egress를 허용하고 RFC1918·Service·link-local 대역을
제외한다. 이 예외를 Codex 실행 Pod에 그대로 승계하지 않는다.

기존 GitHub Actions bot marker는 branch 생성과 함께 만들어졌다. Phase 1에서 marker를
새로 쓰는 구현은 범위 밖이다. executor가 실제 코드를 실행하는 다음 단계 전에, marker의
작성 주체와 `base_dev_sha`를 포함한 신뢰·검증 계약을 Pod 생성 주체에 맞게 별도로
갱신해야 한다.

## 소유 경계

- `Autoresearch`: 기준 SHA를 Experiment에 저장하는 API·migration, GitHub App token
  발급·Git refs 클라이언트, launcher와 executor 이미지, Job 입력 검증, branch 생성의
  멱등성 검증을 소유한다.
- `Autoresearch-infra`: CronJob, ServiceAccount, RBAC, GitHub 자격 증명 보관·주입,
  initContainer·memory volume·NetworkPolicy와 ResourceQuota를 소유한다.
- `Autoresearch-airflow`: 이 Phase 1에서 관여하지 않는다. candidate SHA가 만들어진 뒤의
  학습·평가 소비 계약은 후속 단계에서 정의한다.

## 구현 검증 조건

- Experiment 수락 뒤 `dev`가 전진해도, 나중에 실행한 Pod의 새 branch 부모는 저장된
  `base_dev_sha`와 정확히 같다.
- 후속 비교 집합 등록 계약으로 동일 SHA가 저장된 100개 Experiment는 동시 실행 상한이
  5여도 모두 저장된 같은 `base_dev_sha`를 Job 입력으로 받는다. Phase 1에서는 이 성질을
  launcher 입력 테스트로만 검증한다.
- `base_dev_sha`가 없거나 형식 오류·조회 불가이면 Git ref를 만들지 않는다.
- 동일한 Job 재시도는 같은 SHA의 기존 ref만 성공으로 인정하며, 다른 ref tip을
  덮어쓰지 않는다.
- executor 애플리케이션 컨테이너에는 GitHub App private key가 mount되지 않고, token은
  메모리 volume의 파일로만 전달된다.
- token, private key, 전체 인증 header가 로그·환경 변수·Git remote에 남지 않는다.
- `auto-experiment` label만으로 GitHub Actions가 exp branch를 만들지 않는다.

## 비범위

- Codex 실행, source checkout 후 코드 수정, 테스트·lint, candidate SHA 생성과 push
- GitHub marker의 새 작성·서명·검증 방식
- 비교 집합 batch 등록 API와 `baseline_cohort_id` migration
- Airflow 학습·평가·승격 흐름
