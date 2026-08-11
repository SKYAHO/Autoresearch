# 컨테이너 진행 단계 수집 계약 (#688)

> 정본 범위: executor Job의 컨테이너 진행 상태를 `experiment_steps`로 옮기는 경계.
> 로그 청크 수집은 `docs/specs/2026-08-09-experiment-log-collector.md`가 정본이다.

## 목적과 비목적

**목적.** 워크벤치 관찰 보드의 **진행 단계** 탭을 채운다. 지금 이 탭은 모든 실험에서
`아직 기록된 작업 단계가 없습니다`만 보여준다 — 읽는 쪽(`ui/views.py` `_render_steps`),
API(`/{experiment_id}/steps`), 테이블(`experiment_steps`)은 전부 있는데 **쓰는 쪽이 없다.**

실험 하나는 33분가량 도는데(#682~#686 실측 13:31 제출 → 14:04 완료) 그동안 화면에는
`에이전트 실행 중` 한 줄뿐이다. 어느 구간에서 시간을 쓰는지, 어디서 멈췄는지 볼 수 없다.

**비목적.** 에이전트가 컨테이너 **안에서** 무슨 작업을 하는지는 다루지 않는다.
`step_kind`의 `FEATURE_ASSEMBLY`·`TRAIN`·`EVALUATE` 같은 의미 단계는 executor만 알 수
있고, 그 경로는 [의미 단계는 왜 지금 하지 않는가](#의미-단계는-왜-지금-하지-않는가)에서
따로 다룬다. 이 spec은 **컨테이너 입도**만 약속한다.

## 쓰는 쪽을 executor가 아니라 수집기에 둔다

`log_collector.py`의 `[비책임]`이 이 자리를 명시적으로 비워 뒀다:

> Step 기록(#559 범위 밖)

같은 모듈이 이미 executor Pod을 밖에서 관찰하고 있고, 그 설계 근거도 같은 docstring에
적혀 있다:

> executor 컨테이너는 건드리지 않으므로 **credential 경계가 유지된다.**

executor가 직접 보고하게 하면 API 토큰을 컨테이너 여럿에 마운트해야 한다. 지금 토큰은
`candidate-finalizer` **하나**에만 있고, 그것조차 저장소가 줄이려는 대상이다
(`launcher/jobs.py:485`):

> **이 container에는 push token과 API token이 함께 mount돼 있다.** … sandbox가
> `danger-full-access`라 코드로 막지 않는다 … 컨테이너를 갈라 없애는 것은 Stage 2의 몫이다.

**관측 기능 하나를 위해 줄이려는 노출면을 넓히지 않는다.** 같은 화면을 토큰 변경 없이
얻을 수 있다.

## 결정 1 — Step은 전이 이력이 아니라 현재 상태의 파생값이다

**이 spec에서 가장 중요한 결정이다.**

수집기는 상주 Deployment(`agent-orchestration-log-collector`)지만 재시작한다. 그리고
`SeqCursor` docstring이 못박았듯 **영속 상태를 두지 않는다**:

> **영속 커서가 아니다.** … 재시작하면 이 표가 비고, 그러면 지금까지처럼 전부 다시
> 계산해 같은 키·같은 내용으로 올린다. 적재분이 불변이므로 결과가 같다.

로그는 불변 청크라 재계산이 자명하게 안전하다. **Step은 가변이라 그렇지 않다.** 순진하게
전이를 누적하면 재시작 후 이미 `COMPLETED`인 단계가 `STARTED`로 되돌아갈 수 있다.

그래서 Step을 **매 tick K8s가 말해주는 현재 상태의 순수 함수**로 정의한다.

- 컨테이너 하나 = Step 행 하나. 전이마다 행을 늘리지 않는다.
- 매 tick 그 시점의 상태를 계산해 그대로 쓴다.
- 프로세스는 아무것도 기억하지 않는다.

재시작해도 K8s에 다시 물어 같은 답을 얻으므로 결과가 같다. 끝난 컨테이너는 재시작 후에도
끝나 있다.

### 상태 매핑

`pod.status.initContainerStatuses` + `containerStatuses`의 `state`에서 계산한다.

| K8s container state | Step status |
| --- | --- |
| `waiting` | `STARTED` |
| `running` | `PROGRESS` |
| `terminated`, `exit_code == 0` | `COMPLETED` |
| `terminated`, `exit_code != 0` | `FAILED` |

`waiting`을 `STARTED`로 두는 이유는 8개 컨테이너가 **initContainer로 직렬 실행**되어
아직 차례가 오지 않은 컨테이너도 이 실험의 계획에 포함되기 때문이다. 목록에서 빼면
"앞으로 무엇이 남았는지"가 화면에서 사라진다.

### 되돌아감은 API가 막는다

`update_experiment_step`이 터미널 확정을 원자적으로 보장한다. 저장된 status가
`COMPLETED`/`FAILED`면 조건부 UPDATE의 `rowcount == 0`이 되고
`_finalized_step_or_conflict`가 처리한다 — **같은 터미널 상태를 다시 쓰면 no-op이고,
다른 값을 쓰려 하면 `StepAlreadyFinalizedError`다.**

즉 파생 규칙이 어긋나도 조용히 되돌아가지 않고 사유 코드로 드러난다.

## 결정 2 — 멱등키는 `{pod_name}:{container}`

로그 키(`log_idempotency_key`)와 같은 이유로 **`pod_name`을 포함한다.** `backoffLimit=1`
재시도로 Pod이 새로 뜨면 같은 컨테이너가 처음부터 다시 도는데, Job 이름만 쓰면 이전 실행의
터미널 Step이 새 실행의 갱신을 막아 버린다.

로그 키와 달리 `seq`가 없다 — 컨테이너당 행이 하나이기 때문이다. 상한 128자에 여유가
넉넉하다.

## 결정 3 — 쓰기는 3분기로 줄인다

`create_experiment_step`은 같은 키에 **다른 내용**이 오면 `IdempotencyConflictError`를
낸다. status가 tick마다 바뀌므로 생성만 반복하면 매번 충돌한다. 다음 순서를 따른다.

1. 키로 기존 Step을 찾는다.
2. 없으면 → `create_experiment_step`으로 현재 status와 함께 만든다.
3. 있고 status가 **같으면** → 아무것도 하지 않는다.
4. 있고 status가 **다르면** → `update_experiment_step`으로 갱신한다.

3번이 중요하다. `create_experiment_log`가 "호출 1회 = 트랜잭션 1회 + SELECT 2회"라
증폭을 경계한 것과 같은 이유다. 컨테이너 8개 × 5초 주기로 33분이면 tick 396회인데,
상태는 대부분 그대로다. 변화가 없으면 쓰지 않는다.

## 결정 4 — fail-open이며 로그 수집과 서로를 막지 않는다

`collect_container_logs`가 이미 선언한 규약을 그대로 따른다:

> **fail-open이다.** 한 컨테이너가 실패해도 나머지를 계속 수집하고, 수집 실패가 실험
> 실행을 막지 않는다 — 관측 때문에 파이프라인이 멈추는 것이 더 나쁘다.

추가로 **Step 수집 실패가 로그 수집을 막지 않고, 그 반대도 아니다.** 한 tick 안에서
두 수집은 서로 독립적으로 시도한다. 같은 `collect_once`의 Job 단위 격리 안에 들어가므로
Job 하나의 실패가 뒤의 Job을 날리지 않는 성질도 그대로다.

사유 코드는 접미사 없는 고정 코드 관례를 따른다: `step_write_conflict`,
`step_finalized_conflict`, `step_collection_failed`.

## 결정 5 — `step_kind`는 `OTHER`, `step_type`은 컨테이너 이름

`step_kind` enum(`FEATURE_ASSEMBLY`·`FEATURE_DERIVE`·`TRAIN`·`EVALUATE`·`OTHER`)은
**ML 작업의 의미 단계**를 가리킨다. 컨테이너는 그 축과 일치하지 않는다 —
`codex-worker` 하나가 피처 조립부터 학습까지 전부 한다. 억지로 매핑하면 나중에 진짜 의미
단계가 들어올 때 같은 이름이 두 뜻을 갖는다.

그래서 컨테이너 입도는 `OTHER`로 두고, 무엇인지는 `step_type`에 컨테이너 이름을 그대로
쓴다(`branch-token-minter`, `codex-worker`, …). 화면은 `step_type`을 표시하므로 사용자가
보는 내용은 달라지지 않는다.

## 화면

`_render_steps`가 status를 **불릿 색으로만** 표현하고 글자로는 내보내지 않는다
(`ui/views.py:456`). 색만으로는 `STARTED`와 `PROGRESS`를 구분하기 어렵고 색각 이상에서는
정보가 사라진다. status를 글자로 함께 표시하고, 한국어 라벨 맵을 추가한다
(`_STEP_STATUS_COLORS`는 있으나 라벨 맵이 없다).

```
진행 단계
● branch-token-minter   완료      13:31:44
● branch-creator        완료      13:31:52
● clone-token-minter    완료      13:32:01
● workspace-preparer    완료      13:32:18
● codex-worker          진행 중   13:55:07
○ candidate-verifier    대기
○ push-token-minter     대기
○ candidate-finalizer   대기
```

## 의미 단계는 왜 지금 하지 않는가

`codex-worker`가 33분 중 대부분을 차지하는데 이 구간은 여전히 `진행 중` 한 덩어리다.
쪼개려면 executor가 직접 보고해야 하고, 그러면 API 토큰을 컨테이너 여럿으로 넓혀야 한다.

**Stage 2(8 → 4/5 컨테이너 재구성) 이후로 미룬다.** 그때 컨테이너 경계가 다시 그려지고
토큰 배치도 함께 정리되므로, 지금 배선하면 두 번 고치게 된다. 그 시점에는 이 spec의
결정 1(파생 상태)과 결정 5(`OTHER` 유지)가 그대로 확장 지점이 된다 — executor가 보고하는
Step은 `step_kind`가 `OTHER`가 아닌 값으로 들어오고, 컨테이너 Step과 같은 탭에서 공존한다.

## 인프라 변경

**없다.** 수집기 Deployment는 이미 떠 있고, RBAC은 Pod 조회·로그 조회 권한을 이미 갖고
있다(같은 `list_pods` 응답에서 컨테이너 상태를 읽는다 — 추가 호출이 없다). DB 테이블과
API도 이미 있다.

## 검증

- 단위: 상태 매핑 함수를 K8s 상태 fixture로 고정한다 — `waiting`/`running`/`terminated`
  성공·실패 4갈래.
- 단위: 같은 status가 연속으로 오면 쓰기를 하지 않는다(결정 3의 3번 분기).
- 단위: 저장된 Step이 터미널일 때 같은 값 재기록은 no-op, 다른 값은 사유 코드로 나온다.
- 단위: Step 수집이 실패해도 같은 tick의 로그 수집이 진행된다(결정 4).
- 실환경: GKE에서 실험 1건을 돌려 진행 단계 탭이 컨테이너 8개를 순서대로 채우는지 본다.

## 알려진 한계

- **입도가 컨테이너다.** 위 [의미 단계](#의미-단계는-왜-지금-하지-않는가) 참조.
- **Pod이 사라지면 갱신이 멈춘다.** Job TTL이 지나 Pod이 GC되면 마지막으로 기록된 상태가
  그대로 남는다. 실행 중에 GC되지는 않으므로 완주한 실험은 터미널 상태로 남고, 문제가 되는
  것은 Pod이 비정상 삭제된 경우뿐이다. 그때는 마지막 상태가 `PROGRESS`로 굳는다.
- **시각은 수집 시각이지 전이 시각이 아니다.** 5초 주기이므로 최대 5초 늦다. 33분짜리
  실험의 진행 표시에는 충분하다.
