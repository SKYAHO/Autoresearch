# 실험 로그 수집기 계약 (#559)

Streamlit 워크벤치의 "원본 로그" 탭을 채우기 위해, executor Pod의 컨테이너 로그를
읽어 `experiment_logs`에 적재하는 상주 수집기의 동작 계약이다.

## 목적과 비목적

**목적**: 실행 중인 실험의 로그가 웹 UI에서 5~10초 지연으로 보이게 한다.

**비목적**:

- Step(`experiment_steps`) 기록 — 로그와 성격이 다르다. 단계 경계는 executor가
  알아야 정확하고, 로그는 밖에서 관측할 수 있다. 이 문서는 로그만 다룬다
- executor 코드 변경 — 한 줄도 건드리지 않는다
- 컨테이너 통합(`2026-08-09-agent-authored-experiment-report.md` §결정 3) 대응 —
  통합 후 컨테이너 이름이 바뀌면 수집 대상 목록만 따라가면 된다
- **워크벤치의 로그 표시 상한** — `ui/views.py`가 최근 30개만 그린다. 8000자 청크
  기준 240KB를 넘으면 앞부분이 화면에서 밀린다. 전체 개수는 함께 표기되므로 데이터
  유실이 아니라 UX 문제이며, 필요해지면 후속 이슈로 다룬다

## 읽기 경로는 이미 있다

새로 만들 것은 **적재 한 구간뿐**이다.

```
executor Pod 컨테이너 로그
  ↓  ← 이 문서가 다루는 수집기 (없던 유일한 조각)
experiment_logs
  ↓  ← 이미 있음: ui/client.py get_logs → state.logs
워크벤치 "원본 로그" 탭
  ↓  ← 이미 있음: ui/views.py logs_tab
```

지금 화면의 *"아직 기록된 원본 로그가 없습니다"*는 정상 동작하는 빈 상태다 — 파이프가
UI까지 연결돼 있고 DB에 행이 0개일 뿐이다. **수집기가 행을 넣기 시작하면 UI 변경 없이
보인다.**

워크벤치는 `POLLING_STATUSES`(RUNNING·EVALUATING)인 동안 **5초 cursor polling**을 한다
(`ui/app.py`). 수집 주기를 5~10초로 두면 체감 지연은 최악 15초다.

`POLLING_STATUSES`가 `EVALUATING`까지 포함한다는 점은 아래 "K8s에서 얻는다" 결정과
같은 방향이다 — UI도 상태 전이 후 한동안을 활성으로 취급한다.

## 왜 이 방식인가

`codex-worker`에 API 토큰을 주는 방식(C안)을 기각했다. Codex가
`--sandbox danger-full-access`로 도는 컨테이너에 쓰기 자격증명이 있으면 자기 실험을
`PASSED`로 만들거나 가짜 로그를 넣을 수 있다. **경계는 유지하고 관측만 밖에서 한다.**

사이드카(B안)도 기각했다. 지연은 더 짧지만 Pod spec을 바꿔야 해서 컨테이너 통합 작업과
겹친다. 이 수집기는 **executor와 무접촉**이다.

## 어디서 도는가

**별도 Deployment의 상주 프로세스**다. CronJob은 최소 주기가 1분이라 5~10초를 낼 수 없다.

- ServiceAccount·`ORCH_DATABASE_URL`은 **launcher와 재사용**한다
- launcher tick 안에 넣지 않는다 — 주기가 1분에 묶이고, 로그 수집 실패가 claim 경로에
  영향을 줄 여지가 생긴다

## API를 거치지 않는다

`launcher.Dockerfile`이 `agent_orchestration/app`을 통째로 포함하고 launcher가 이미
`Session`으로 DB에 쓴다. 수집기도 같은 이미지·같은 방식이므로
`app.experiments.service.create_experiment_log(session, experiment_id, request)`를 직접
호출한다.

**따라서 API 토큰·NetworkPolicy egress·HTTP 재시도 정책이 전부 불필요하다.**
`ORCH_EXECUTOR_API_URL`·`ORCH_EXECUTOR_API_TOKEN_SECRET_NAME`은 launcher가 executor Job에
넘겨주려고 들고 있는 값이지 launcher 자신이 쓰는 것이 아니다.

## 수집 대상 찾기

```
1. Job 목록      label_selector = "app.kubernetes.io/component=experiment-executor"
2. Pod 목록      label_selector = "job-name=<job_name>"        (K8s가 자동 부여)
3. experiment_id  Job 이름 ar-exec-<32 hex>에서 복원
```

### 1번은 K8s에서 얻는다 — DB가 아니다

`BatchV1Api.list_namespaced_job`으로 매 주기 조회한다. launcher와 같은
`EXPERIMENT_EXECUTOR_LABEL_SELECTOR` 상수를 재사용해 두 곳이 갈리지 않게 한다.

DB에서 `RUNNING` experiment의 `executor_job_name`을 읽는 방식을 쓰지 않는 이유:

- **수집 대상은 "로그가 존재하는 Pod"이지 "DB가 RUNNING이라고 믿는 실험"이 아니다.**
  상태 전이가 늦거나 어긋나도 로그는 Pod에 남는다. 실제로 `finalizer`가
  `candidate_sha`를 보고해 `EVALUATING`이 된 뒤에도 같은 Job이 candidate 학습·측정을
  계속한다(2026-08-09 `6ec09890` 관측) — DB 상태로 거르면 그 구간을 통째로 놓친다
- **`LogSink`가 쓰기 전용으로 남는다.** DB 조회를 넣으면 어댑터 하나가 읽기·쓰기를
  겸하게 되고 프로토콜 경계가 넓어진다

종료된 Job도 TTL(3600초) 동안 목록에 남는데 이는 **필요한 성질**이다 — 완료 직후
마지막 부분 청크를 flush하려면 종료 여부를 알아야 하고, 그 판정을 Job·Pod 상태에서
얻는다.

`job-name`은 Job이 만든 Pod에 Kubernetes가 자동으로 붙이는 label이다.

**`CoreV1Api`가 새로 필요하다.** 현재 launcher는 `BatchV1Api`만 쓰고 `JobClient`
프로토콜에도 `count_active`·`get`·`create`뿐이라 Pod을 찾는 경로가 없다.

**`JobClient`에 얹지 않고 별도 어댑터로 분리한다** — Job 생성과 로그 조회는 책임이
다르고, 섞으면 테스트 더블부터 꼬인다.

### Pod이 0개이거나 2개 이상일 때

| 개수 | 판정 | 처리 |
|---|---|---|
| 0개 | Job 생성 직후 스케줄링 지연 — **정상** | 조용히 skip, 다음 주기에 다시 |
| 1개 | 정상 | 수집 |
| 2개 이상 | `backoffLimit=1` 재시도로 발생 가능 (`jobs.py`) | `creationTimestamp` **최신 하나만** 채택 |

이전 Pod의 로그는 이미 적재된 것이 그대로 남는다 — 아래 멱등키에 `pod_name`이 들어가
새 Pod의 청크와 섞이지 않는다.

## 증분 읽기 — 시간창이 아니라 오프셋

`create_experiment_log`는 **같은 `idempotency_key`에 다른 내용이면
`IdempotencyConflictError`를 낸다**(같은 내용이면 기존 row를 그대로 돌려준다).

그래서 `since_seconds`류 시간창을 쓸 수 없다. 같은 창을 다시 읽으면 그 사이 로그가 자라
내용이 달라지기 때문이다.

**컨테이너 로그가 append-only라는 성질**을 쓴다.

```
1. read_namespaced_pod_log(pod, container)      전체를 읽는다
2. 8000자 경계로 자른다                           앞부분은 절대 안 바뀌므로 경계가 고정된다
3. **완성된 청크만** 올린다                        마지막 부분 청크는 보류
4. 컨테이너가 종료되면 마지막 부분 청크도 올린다
```

**3번이 핵심이다.** 아직 자라는 중인 마지막 청크를 올리면, 다음 주기에 같은 키·다른
내용이 되어 충돌한다. 8000자가 찬 청크만 올리면 **모든 적재분이 불변**이 되어 재실행·
재시작에 안전하다. 상태를 따로 저장할 필요가 없다.

**절단 기준은 문자 수다.** `content` 제약이 `max_length=8192`로 문자 기준이므로 byte가
아니라 문자로 자른다. 8000으로 두는 것은 여유분이다.

### 트레이드오프 — 매 주기 전체를 다시 읽는다

오프셋 캐시를 두지 않으므로 주기마다 kube-apiserver에서 로그 전체를 다시 받는다.
누적 전송량은 대략 `로그크기 × 주기수 / 2`다.

`ORCH_ACTIVE_DEADLINE_SEC`가 60000(**16.7시간**)이므로 상한까지 도는 Job이 있다면
수백 MiB가 될 수 있다. 다만 실측된 완주 시간은 **31분·33분**(2026-08-09, 실험 #634·#635)로
상한의 3% 남짓이라 현재 규모에서는 무시할 수준이다.

**오프셋 캐시는 실측 후에 넣는다.** 근거 없이 상태 관리를 늘리지 않는다.
다음 실행 때 **컨테이너별 로그 크기와 실제 소요 시간을 함께** 측정해 이 전제를 다시 본다
— 컨테이너 통합으로 Codex 호출이 2회가 되면 늘어난다.

## 멱등키

```
{pod_name}:{container}:{seq}
```

`pod_name`이 `job_name`을 접두사로 포함하므로 Job 식별력을 잃지 않으면서 짧다.

```
pod_name    ar-exec-6ec09890a4a84c699760c01349351505-x7k2   46자
container   candidate-finalizer                             19자
seq         0~9999                                           4자
구분자                                                        2자
─────────────────────────────────────────────────────────────
합계                                                     최대 71자   (상한 128자)
```

`{job_name}:{pod_name}:{container}:{seq}` 형태는 112자로 여유가 16자뿐이라 쓰지 않는다.

**`pod_name`을 반드시 포함해야 한다.** 재시도로 Pod이 새로 뜨면 로그가 처음부터 다시
시작되어 `seq`가 겹치는데, `pod_name`이 없으면 서로 다른 실행의 청크가 같은 키를 갖게
된다. 내용이 달라 `IdempotencyConflictError`로 터지긴 하지만, **정상 재시도가 오류로
보이는 것 자체가 결함**이다.

`log_type`에는 컨테이너 이름을 넣는다(상한 32자, 가장 긴 `candidate-finalizer`가 19자).
UI에서 어느 단계의 로그인지 구분된다.

## 오류 분류 — catch-all을 쓰지 않는다

8-container 순차 실행이라 **뒤 컨테이너가 아직 없는 것이 정상**이다. 이를 다른 실패와
같이 묶으면 진짜 오류가 묻힌다.

| 상황 | 판정 | 처리 |
|---|---|---|
| `ContainerCreating`·`PodInitializing` (400) | 정상 | 조용히 skip |
| Pod·컨테이너 없음 (404) | 정상 | 조용히 skip |
| 그 외 `ApiException` | 이상 | 고정 사유 코드로 로그 남기고 skip |
| `IdempotencyConflictError` | 이상 | 고정 사유 코드로 로그 남기고 skip — 키 규칙 결함 신호다 |
| DB 오류 | 이상 | 고정 사유 코드로 로그 남기고 skip |

**전체 정책은 fail-open이다.** 수집 실패가 실험 실행을 막지 않는다. 관측 때문에
파이프라인이 멈추는 것이 더 나쁘다.

사유 코드는 `executor/phase2.py`의 `_safe_failure_reason` 관례를 따라 접미사 없는
`^[a-z][a-z0-9_]*$` 고정 코드로 남긴다.

## 필요한 인프라 변경 (`SKYAHO/Autoresearch-infra`)

**이 저장소 범위 밖이며 착수 전 승인이 필요하다.**

1. **RoleBinding** — `experiment-job-observer` Role(`autoresearch-experiments`,
   `pods/log: get`·`pods: list,watch,get` 포함)을 수집기 ServiceAccount에 바인딩한다.
   Role은 이미 존재하므로 RoleBinding만 추가한다
2. **Deployment** — 상주 수집기. SA와 `ORCH_DATABASE_URL`은 launcher와 재사용한다

**NetworkPolicy 추가는 불필요하다.** launcher 정책에 kube-apiserver(443)로 나가는 경로가
이미 열려 있고, Pod 로그 조회도 같은 경로다. DB 접근도 launcher와 같다.

## 검증

- [ ] 실험 1건이 도는 동안 워크벤치 "원본 로그" 탭에 로그가 시간순으로 쌓인다
- [ ] 같은 컨테이너의 청크가 중복 row로 늘어나지 않는다 (수집기 재시작 후에도)
- [ ] 컨테이너가 아직 안 뜬 구간에서 오류 로그가 남지 않는다 (정상 skip)
- [ ] 수집기를 죽여도 실험은 계속 완주한다 (fail-open)
- [ ] 재시도로 Pod이 둘이 된 실험에서 두 Pod의 로그가 섞이지 않는다
- [ ] 컨테이너별 로그 크기와 실제 Job 소요 시간을 측정해 오프셋 캐시 필요 여부를 판단한다

## 알려진 한계

- **지연은 폴링 주기만큼이다.** 진짜 스트리밍(초 미만)이 필요하면 사이드카(B안)를 다시
  검토해야 하고, 그때는 Pod spec 변경이 따른다
- **Step 기록은 이 문서 범위 밖이다.** 단계 경계는 밖에서 관측하기 어렵다 — executor가
  직접 보고해야 정확하고, 그것은 credential 배치 결정이 선행돼야 한다
