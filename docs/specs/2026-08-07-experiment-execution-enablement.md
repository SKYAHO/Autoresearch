# 실험 실행 능력 활성화 계약

## 목적

이 문서는 배포된 executor가 **실험을 실행하지 않는** 현재 상태를 끝내고, 에이전트가
실험 하네스를 설계·구현하는 능력을 서비스에서 관측 가능하게 만드는 계약이다.

현재 파이프라인은 `코드 수정 → ruff/pytest → push`만 수행한다. 학습도 ROC-AUC 측정도
판정도 일어나지 않는다. 그 결과 `candidate_sha`가 채워진 실험이 하나도 없고
(2026-08-07 dev DB 실측: RUNNING 7·ERROR 3·CREATED 13 전부 `None`), 검증 파이프라인은
통과시킬 대상을 받아본 적이 없다.

이 문서는 **무엇을 켜고 무엇을 미루는지**의 경계를 고정한다. 구현 순서는
`docs/plans/2026-08-07-experiment-execution-enablement.md`가 소유한다.

## 진단 — 현재 막힌 지점

2026-08-07 dev 클러스터(`autoresearch-dev-gke`)와 코드베이스 실측 결과다.

| 지점 | 사실 | 근거 |
|---|---|---|
| 학습 미실행 | `ORCH_TRAINING_DATASET_PATH` 미설정 → 조용히 skip | `phase2.py:196` |
| 스냅샷 읽기 불가 | `experiment-job` GSA가 `objectCreator`만 보유 | `phase2.py:186-191` 주석 |
| Job 실패 지점 | 8-container 중 6번째 `candidate-verifier` | `verifier.py:454` |
| 실패 사유 | `no_changes` — Codex가 exit 0인데 변경 0건 | Cloud Logging |
| 원인 규명 불가 | codex-worker가 Codex 출력을 폐기 | `codex_worker.py:294` |
| 보고 불가 | 5·6번 컨테이너에 API credential 미마운트 | `launcher/jobs.py` |
| 관측 불가 | `experiment_logs`·`experiment_steps` 전 실험 공백 | dev DB 실측 |

`no_changes`는 버그가 아니라 설계대로의 fail-closed다. Codex 프롬프트가 *"구현할 수
없으면 변경 없이 종료해 verifier가 fail-closed하게 하라"*고 지시한다. 문제는 **왜
포기했는지 알 수 없다는 것**이다.

## 목표와 비목표

**목표**: 에이전트가 가설을 받아 실험 하네스를 설계·구현하고, 그 하네스가 실제 데이터로
학습·측정하며, 결과를 사람이 검토할 수 있는 형태로 남기는 한 바퀴.

**비목표 (이 계약 범위 밖)**:
- 완전 자율 실험. 1단계는 사람이 Step API로 관찰하는 반자동이다
- 피처 정의 변경 실험. 아래 §스냅샷 정책 참조
- executor Job을 대체하는 새 실행 경로. 컨테이너 구성은 아래 §컨테이너 구성 범위에서
  조정하되 Job 모델 자체는 유지한다

## 근거 — 로컬 실증에서 확인된 것

2026-08-03 노트북 세션(round_004) 조사 결과가 이 계약의 근거다. 구두 설명과 실제가
달랐던 부분이 설계 판단을 바꿨다.

1. **에이전트에게 피드백 루프는 없었다.** `search_grid.py`(5피처×7모델=35칸)를 한 번
   쓰고 한 번 실행했다. 탐색은 스크립트 안에서 일어났다. 증명된 능력은 "반복 학습"이
   아니라 **"실험 하네스 설계·구현"**이다.
2. **에이전트의 첫 결론이 틀렸다.** val 1위가 9자리까지 동점인데 test가 높은 쪽을 고른
   자기 규칙 위반이었다. 정정은 사람의 질문("피쳐만 바꿨을때 진 이유는?") 이후 일어났다.
   → **판정을 코드로 고정한 현행 설계가 옳다는 실증이다.**
3. **피처 기여는 노이즈였다.** 10분할 분해: 모델만 +0.0577(유의), 피처만
   +0.0043(노이즈), 상호작용 −0.0027. 지시문이 요구한 두 축 중 피처 축은 소득이 없었다.
4. **보조 지표가 악화됐다.** LogLoss 0.0875→0.1671, Brier 0.0132→0.0479. ROC-AUC 단일
   지표 판정의 맹점이 드러났다.
5. **데이터가 결론을 지탱하지 못했다.** 로컬 CSV 2,400행·양성 36건(1.5%). 정규 경로
   (`build-features`)가 feast 미설치·GCP 인증 부재로 60초 hang 후 timeout하여 CSV로
   우회했고, 그 CSV의 출처·기간·생성 명령 기록이 없다.

5번이 이 계약이 필요한 이유다. **서비스에는 우회로가 없어야 한다.**

## 스냅샷 정책 — 공유 우선, 실험별은 후속

학습 입력 스냅샷을 실험 간 공유할지 실험마다 새로 뽑을지는 다음과 같이 고정한다.

**1단계(이 계약): 공유 스냅샷.**

- 조립은 executor Pod **밖**에서 수행하고, Pod은 `by-hash/<sha256>/` 스냅샷을 읽기만 한다
- Pod 안 조립이 불가능한 이유는 의존성이다. `pyproject.toml`의
  `conflicts = [[{group="feast"}, {group="dev"}], ...]` — executor 이미지는 pytest 검증을
  위해 dev 그룹이 필요한데 feast와 배타적이다. 재빌드로도 넣을 수 없다
- 따라서 `feature_change_unsupported`(`phase2.py:203`) 제약은 **유지된다**
- 에이전트는 피처를 하네스 내부 파생 함수로만 만든다. 로컬 실증이 실제로 이 방식이었다

**2단계(후속): 실험별 registry.**

실험별 Feast Registry 격리는 **계약이 이미 존재한다**(#454,
`docs/specs/2026-07-31-experiment-isolated-offline-run.md`):

```
gs://<registry-bucket>/experiments/<issue>/<experiment_id>/<condition>/<source_sha>/registry.db
```

- `autoresearch/experiments/context.py:build_experiment_context` — 좌표 생성 완료
- `paired_experiment.py` — `REGISTRY_URI_MISMATCH` fail-closed 검증 완료
- **남은 것은 실행 Job 배선**이며 `Autoresearch-airflow`·`Autoresearch-infra` 소유다.
  이 저장소의 `dags/`는 비어 있고 `autoresearch/jobs/`에 `feast apply` 실행 코드가 없다

1단계를 먼저 하는 이유는 실패 원인 분리다. executor가 한 번도 완주한 적 없는 상태에서
조립까지 얹으면 조립·학습·판정 세 층의 실패가 겹쳐 원인 규명이 불가능하다.

## 계약 — 다섯 덩어리

### B. 데이터·학습 활성화

`ORCH_TRAINING_DATASET_PATH`가 설정되면 `phase2.py`의 기존 호출부가 그대로 동작한다.
새 코드가 아니라 **배선과 권한**이 범위다.

- 사전: 조립 Job이 `publish_snapshot()`으로 `by-hash/<sha256>/` 게시
- IAM: `experiment-job` GSA에 스냅샷 root **read** 부여 (현재 `objectCreator`만)
- launcher가 executor Job에 dataset URI 주입
- baseline은 Codex 실행 **전**, candidate는 push **후**. 순서는
  `training.py:220`의 marker가 강제한다 — 뒤집히면 baseline이 candidate의 의존성으로
  학습돼 paired 대조의 전제가 깨진다

**시간 예산 재계산이 필수다.** 현재 `POLICY_SEEDS=3`은 통계적 근거가 아니라 Job 시간
상한에서 역산된 값이다(학습 1회 67.8초 × 30 seed × 2조건 = 4,068초 >
`activeDeadlineSeconds` 3600초). 학습을 켜는 순간 이 예산이 실제로 소비된다.

### D. 관측성

"에이전트가 하네스를 설계·구현했다"를 **보이게** 하는 것이 이 덩어리의 목적이다.

- codex-worker가 최소 진단을 남긴다: exit code, 소요 시간, 변경 파일 수, stdout 말미
  요약. 원문 전체는 여전히 남기지 않는다(프롬프트·워크스페이스 유출 방지)
- Step API(`FEATURE_ASSEMBLY`/`TRAIN`/`EVALUATE`, #518)에 진행 상황을 기록한다.
  계약은 이미 있고 현재 사용처가 없다
- Pod TTL 30초(`ORCH_TTL_AFTER_FINISHED_SEC`) 때문에 `kubectl logs`가 불가하므로,
  DB 기록이 유일한 사후 관측 경로다

### 컨테이너 구성 범위

현행 8-container 분해는 **토큰 minter 3개만 분리를 유지하고, credential이 없는 나머지
스테이지는 통합**한다. D와 함께 수행한다.

```
[분리 유지] branch / clone / push token-minter   ← private key가 이 셋에만 들어감
[통합]      workspace-preparer + codex-worker + candidate-verifier
            + 학습 + candidate-finalizer
            → 토큰 볼륨은 필요한 시점에만 마운트
```

**보안 속성은 컨테이너 개수가 아니라 볼륨 마운트가 만든다.** 토큰 볼륨을 붙이지 않은
컨테이너 1개는 붙이지 않은 컨테이너 3개와 같은 수준이다. 분리가 추가로 사는 속성은
"Codex가 이미지 파일을 오염시켜 다음 스테이지가 그것을 실행하는 것"의 차단인데,
`_restricted_container_security_context()`가 이미 `read_only_root_filesystem=True`를
설정하므로 Codex는 workspace 볼륨 외에 쓸 수 없다. 따라서 분리로만 얻는 고유 이점이
실질적으로 남지 않는다.

통합이 사는 것:

1. **반복이 가능해진다.** initContainer 체인은 직선이라 되돌아갈 수 없다. 에이전트가
   하네스를 돌려보고 실패를 고치는 경로가 구조적으로 봉쇄되어 있다. 로컬 실증에서도
   `NameError`·`AttributeError`로 3회 재실행이 필요했다
2. **에이전트가 자기 실험을 안다.** 현재 `experiment_id=unknown`으로 컨텍스트가
   컨테이너 경계에서 끊긴다
3. **Step 보고가 자연스러워진다.** 현재 5·6번은 API credential이 없어 보고 자체가 불가능하다
4. **재시도 단위가 맞아진다.** 현행 Job `backoffLimit` 재시도는 Codex를 통째로
   재호출한다(2026-08-07 실측: branch-creator `created=False`로 2회 실행 확인).
   LLM 호출은 비싸고 비결정적이라 최악의 재시도 단위다
5. **학습 배치가 정직해진다.** 현재 baseline 학습이 `workspace-preparer` 안에,
   candidate 학습이 `candidate-finalizer` 안에 들어가 있다(`phase2.py:181`, `:301`) —
   컨테이너를 늘리지 않으려고 기존 스테이지에 끼워넣은 결과이며 이름과 동작이 어긋난다

B(Stage 1)는 통합 없이도 가능하므로 먼저 수행한다. 통합을 Stage 1과 겹치면 학습 실패와
통합 결함이 뒤섞여 원인 규명이 어려워진다.

### A. 산출물 계약

에이전트의 산출물을 **제품 코드 diff**에서 **실험 하네스 + 결과 JSON**으로 바꾼다.

| | 현행 | 변경 후 |
|---|---|---|
| 산출물 | `src/`·`autoresearch/` diff | 격리 디렉터리의 하네스 + 결과 |
| 검증 | ruff/pytest + 변경 있음 | 하네스가 실행됐고 결과가 계약을 지켰는가 |

로컬 실증이 이미 이 형태였다 — `capability_probe/round_004_.../` 하위에만 커밋했고
제품 코드는 건드리지 않았다. 이 패턴을 계약으로 승격한다.

### C. 판정 확장

기존 paired t-test 엔진을 유지하되 입력을 "에이전트가 지목한 우승 조합"으로 바꾸고,
로컬에서 드러난 두 결함을 정책에 반영한다.

- **기여 분해 필수**: 피처만/모델만/상호작용. 없으면 "피처 가설 성공"으로 잘못 기록된다
- **보조 지표 가드**: LogLoss·Brier가 임계 이상 악화되면 ROC-AUC 개선과 무관하게 기각
- 판정 계층은 에이전트의 자체 측정치를 **믿지 않고 독립 재현**한다. 사람이 수행했던
  "다시 리뷰해봐"의 역할을 코드가 대신한다

### E. 시간·재시도 모델

로컬 실측: 전체 3시간 18분 중 순수 학습 계산은 172초(1.5%)였다. 나머지는 에이전트
추론·작성 시간이다.

- `activeDeadlineSeconds=3600`은 에이전트 사고 시간을 감당하지 못한다
- Job `backoffLimit` 재시도가 Codex를 통째로 재실행한다(2026-08-07 실측: 2회씩 실행)
- **하네스 작성(길고 쌈)과 하네스 실행(짧고 비쌈)의 분리**를 검토한다
- 다만 구체 수치는 B·D 완료 후 실측으로 정한다. 지금 정하면 추측이다

## 유지되는 것

다음은 이 계약이 바꾸지 않는다. 진단 결과 타당한 설계로 확인됐다.

- 목적별 토큰 발급(branch/clone/push)과 credential 최소 마운트 — minter 컨테이너 분리는
  유지한다
- LLM에게 push 토큰·prod 자격증명을 주지 않는 원칙
- `read_only_root_filesystem=True`, `allow_privilege_escalation=False`,
  `capabilities.drop=["ALL"]`, `automount_service_account_token=False`
- `base_dev_sha` 봉인에 의한 기준선 고정
- 통계 판정 엔진을 LLM에서 분리해 코드로 고정한 것
- fail-closed 일관성(`HOLD`를 성공으로 취급하지 않음)
- 사람의 승격 게이트(`PROMOTED` 스키마 배제 + `reason` 필수)

## 미해결 위험

- **에이전트 자기 판단의 신뢰도.** 로컬에서 첫 결론이 틀렸고 사람이 잡았다. C의 독립
  재현이 이 위험을 흡수하도록 설계해야 한다
- **표본 크기.** 로컬 결론은 양성 36건 위에서 나왔다. 서비스 스냅샷의 최소 표본 요건을
  정해야 하며, 커버리지 가드(`CTR_TRAINING_MIN_COVERAGE_DAYS`,
  `MIN_ROWS_PER_DAY`)만으로 충분한지 검증되지 않았다
- **이슈 폼과 실제 능력의 불일치.** 폼은 *"Feast 정의 수정을 허용한다"* 체크박스를
  제공하지만 1단계에서는 학습이 거부한다. 에이전트에게 전달되는 `allowed_scope`가
  실제 실행 가능 범위와 어긋나지 않게 맞춰야 한다
