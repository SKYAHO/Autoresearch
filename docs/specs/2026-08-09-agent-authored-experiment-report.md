# 에이전트가 리포트를 쓰는 실험 실행 — MVP 계약

> 2026-08-09 | 상태: 초안
>
> `docs/specs/2026-08-07-experiment-execution-enablement.md`의 **A(산출물 계약)·
> C(판정 확장)·컨테이너 구성 범위**와 그 plan의 **Stage 4·5·6**을 대체한다.
> 진단(§현황)과 B(데이터·학습 활성화)·E(시간·재시도)는 그대로 유효하다.

## 요약

실험의 산출물을 **`report.md`**로 정의하고, 그것을 **에이전트가 직접 쓰게** 한다.
통계 게이트를 승격 관문에서 제거하고, 판정은 (1) 파이프라인이 계산한 지표와
(2) 별도 에이전트의 리뷰, (3) 사람의 최종 결정으로 옮긴다. executor는 컨테이너
8개에서 **4개**로 재구성한다.

## 배경 — 왜 바꾸는가

2026-08-08 실험 #619가 executor를 처음 완주했다. 그 결과 두 가지가 드러났다.

**① 측정한 것이 남지 않는다.** 학습 산출물이 emptyDir에 있어 Pod TTL 후 소멸한다.
`status=EVALUATING`은 "평가 중"이 아니라 "평가를 기다리는 중"이고, 평가할 입력이
없다. `metric_summary=null`, `steps=0건`이다.

**② 기존 판정 계약이 다른 실행 모델용이다.** `paired-offline-experiment-v1`은
Airflow가 조건별 격리 Job을 띄우고 코드를 GCS 아카이브로 굽고 조건별 Feast
registry를 두는 모델을 전제한다. Phase 2 executor는 그중 아무것도 하지 않는다.
`experiment_id`조차 형식이 다르다(executor는 UUID, 계약은 슬러그
`^[a-z0-9][a-z0-9-]{0,31}$`).

계약을 충족시키는 것은 배선이 아니라 **Pod 안에 다른 실행 모델을 재구현하는 일**이다.

## 결정 1 — 통계 게이트를 승격 관문에서 제거한다

`evaluate_experiment` / `decide_promotion`의 고정 규칙(`평균 delta > 0` AND
`95% CI 하한 > 0`)을 PASSED/FAILED를 정하는 관문으로 쓰지 않는다.

**근거:**

- **현재 설정에서 통계적 엄밀성이 없다.** `POLICY_SEEDS = tuple(range(42,45))` →
  자유도 2 → `t_critical_95(2) = 4.303`. 신뢰구간 반폭이 표준오차의 4.3배다.
  게다가 seed 3은 통계적 근거가 아니라 **Job 시간 상한에서 역산된 값**이다
  (`2026-08-07` spec §B).
- **막고 있는 것이 없다.** 승격은 어차피 사람이 `report.md`를 보고 결정한다.
  게이트는 안전장치가 아니라 라벨러다.
- **규칙이 무한히 증식한다.** 캘리브레이션 붕괴를 잡으려면 LogLoss·Brier 임계
  규칙, 기여 분해 규칙이 필요하고, 새 실패 모드마다 규칙이 하나씩 붙는다.
- **충족 비용이 값보다 크다.** 위 §배경 ②의 항목 전부가 이 게이트 때문에 필요했다.

**더 이상 필요 없어지는 것:** `plan_receipt`(write-once GCS 영수증), 조건별 Feast
`registry.db`, 코드 GCS 아카이브, MLflow Registry 모델 등록, UUID/슬러그 식별자 정합,
`paired-offline-experiment-v1` 충족.
→ **`2026-08-07` plan의 Stage 6이 통째로 불필요해진다.**

**MLflow tracking 배선(#624/#626)은 그대로 둔다.** 판정이 `runs:/<run_id>/...`
artifact에 의존하지 않게 되었을 뿐, 학습 run 기록 자체는 값이 있다. **판정 경로에서
load-bearing이 아니게 되는 것이지 제거 대상이 아니다.**

**대체되는 것:** `2026-08-07` plan Stage 5의 코드 렌더러(#620,
`src/pipeline/experiment_report_markdown.py`)는 `PairedExperimentResult`를 입력으로
받는다. 이 계약에서는 `report.md`를 **에이전트가 쓰므로**(§결정 2) 그 입력이 생기지
않는다.

**유지되는 것:** 통계 **함수**(`compare_to_baseline`, `summarize_metric`,
`t_critical_95`)는 계속 쓴다. 판정 관문이 아니라 `report.md`에 실을 **참고 수치**로
계산한다. `src/pipeline/paired_experiment.py`의 계약 층만 이 경로에서 쓰지 않는다.

## 결정 2 — 계산과 판정을 분리한다

| | 누가 | 무엇을 |
|---|---|---|
| **측정** | 파이프라인이 기존 평가 코드를 실행 | seed별 baseline·candidate 지표를 계산해 `metrics.json`을 낸다. **판정하지 않는다** |
| **서술** | Codex (실험을 수행한 에이전트) | `metrics.json`을 읽고 `report.md`를 쓴다 |
| **리뷰** | Claude (별도 에이전트) | `report.md`를 `metrics.json`·diff와 대조한다 |
| **승격** | 사람 | `report.md`를 보고 결정한다 |

**측정은 새 모듈이 아니라 기존 `src/pipeline/evaluate.py`를 쓴다.** 이미 ROC-AUC ·
LogLoss · Brier · PR-AUC · grouped ROC-AUC를 계산하며, executor가 `train-model`을
호출하는 것과 같은 패턴으로 `evaluate-model`을 호출한다.

**두 조건을 같은 채점자가 채점하므로 candidate 코드로 평가해도 delta는 유효하다.**
채점 경로를 손대면 diff에 드러나 리뷰가 잡는다 — 코드 레벨 봉인을 겹치지 않는다
(§결정 3의 격리 중복 선례 참조).

**리뷰어에게 주는 것과 안 주는 것:**

| 준다 | 안 준다 |
|---|---|
| 이슈의 가설 원문 | 리뷰 대상에 대한 쓰기 권한 |
| `metrics.json` (파이프라인이 계산한 수치) | 자기 출력을 다시 읽는 경로 |
| candidate diff (사실로서) | |
| `report.md` (주장으로서) | |

리뷰어가 답할 질문: ① 가설이 말한 것을 실제로 구현했는가 ② 라벨 리크·테스트셋
오염·시점 누수가 있는가 ③ 두 조건의 측정 조건이 같은가 ④ 지표 상승이 가설이 말한
메커니즘에서 왔는가 ⑤ 주 지표가 못 보는 손상(캘리브레이션 등)이 있는가.

**①·④는 통계가 답할 수 없고, ②·③은 통계가 오히려 속는다** — 리크된 실험은
paired t-test를 가장 깨끗하게 통과한다.

**주의:** `report.md`와 diff는 **에이전트가 쓴 텍스트**다. 리뷰어 지침은 코드 주석과
리포트 서술을 사실이 아니라 **주장**으로 취급해야 한다. 가능한 항목은 추론이 아니라
검사로 답한다(예: 테스트셋 오염은 멤버십 해시 대조).

## 결정 3 — executor를 4 컨테이너로 재구성한다

```
① token-minter        branch·clone 토큰 발급              App private key
② worker              clone → baseline 학습 → Codex #1
                      → 검증 → candidate 학습 → 측정
                      → Codex #2(report.md)               credential 없음
③ push-token-minter   push 토큰 발급                      App private key
④ finalizer           commit → push → GCS 게시 → API 보고  push·API 토큰
```

**credential 지도:**

| 자격증명 | 어디에 |
|---|---|
| GitHub App private key | ①, ③ 에만 |
| branch·clone 토큰 | ② 에만 |
| push 토큰 · API 토큰 | ④ 에만 |
| Codex 실행 컨테이너(②)의 GitHub·API 자격증명 | **없음** |

> **미결 (2026-08-09) — 이 표의 두 줄이 서로 모순이다. Stage 2 착수 전에 정한다.**
>
> ②는 clone을 하므로 clone 토큰이 마운트돼야 하고, 같은 컨테이너에서 Codex가
> `--sandbox danger-full-access`로 돈다. 즉 "Codex 실행 컨테이너에 GitHub 자격증명
> 없음"이 4 컨테이너 구성에서는 성립하지 않는다.
>
> **이 모순은 아래 "verifier의 정책 강제를 하네스로 대체한다" 절의 근거를 무너뜨린다.**
> 거기서 credential 내용 검사(`_content_is_forbidden`)를 걷어내는 이유가 "②엔 토큰이
> 마운트되지 않아 복사할 대상이 존재하지 않는다"인데, ②에 clone 토큰이 있으면
> 대상이 생긴다. 토큰이 들어온 컨테이너에서 토큰 유출 검사를 떼는 셈이 된다.
>
> 기록상 prepare를 ②에 합친 근거는 "`.git` 마운트 충돌이 사라져 **넣을 수 있다**"
> 하나뿐이고(아래 5 → 4), clone 토큰이 Codex 쪽으로 넘어온다는 사실은 계산되지
> 않았다. `.git` 마운트 검사 제거와 prepare 흡수는 별개 판단이다 — 전자만 해도
> branch-creator·verifier 흡수라는 실익은 그대로 얻는다(8 → 5).
>
> 선택지: **(가) prepare를 살려 5 컨테이너** — 표의 두 줄이 모두 사실이 된다.
> 비용은 컨테이너 기동 시간뿐이고 workspace 볼륨은 어차피 공유라 핸드오프 추가
> 비용이 없다. **(나) 4 유지 + clone 직후 토큰 파일 unlink** — 소비 측 마운트가
> `read_only=True`라 그것부터 풀어야 하고, 순서 의존이라 예방보다 약하다.
> **(다) 4 유지 + 표와 verifier 절을 사실에 맞게 수정** — clone 토큰이 Codex에
> 노출됨을 받아들이고 credential 검사는 남긴다.
>
> 참고: `workspace.py`는 토큰이 workspace로 새지 않도록 이미 설계돼 있다 —
> `_clean_remote_url()`, 환경변수 + 일회성 `GIT_ASKPASS`, clone 후
> `remote.origin.url`·`credential.helper` 대조. 그래서 (가)에서는 볼륨을 마운트하지
> 않는 것만으로 격리가 성립한다.

**push 토큰은 Codex가 끝난 뒤(③)에 발급된다.** 순서로 보장되던 성질을 유지한다.

**단, GCS 접근은 다르다.** `exp-job` GSA는 Workload Identity로 **Pod 단위**에 붙으므로
②에서도 GCS에 닿는다(같은 Pod에서 데이터셋 다운로드가 이미 동작한다). 컨테이너를
어떻게 나누든 Codex는 GSA 권한 범위 안에서 GCS를 쓸 수 있다. 이것이 문제가 되지
않는 이유는 그 권한이 `objectCreator`(생성만, 교체 불가)이기 때문이다 —
**경계는 컨테이너가 아니라 IAM이 만든다.** 컨테이너를 8개에서 4개로 줄이는 판단이
안전을 깎지 않는 근거이기도 하다.

### `.git` read-only 마운트를 제거한다

현행 `codex_worker._capture_protected_git_metadata`는 `/proc/self/mountinfo`를 읽어
`.git`이 커널 read-only 마운트인지 확인하고, 아니면 `git_metadata_unprotected`로
거부한다. 이 **마운트 검사만** 제거하고 digest 대조는 유지한다.

- **잃는 것:** `.git/hooks/*` 등을 심는 시점의 예방. 다만 `_git_metadata_digest`가
  HEAD·refs·config·**hooks**를 포함해 전후 대조하므로 ②에서 fail-closed로 끊긴다.
  심어진 hook이 실행될 수 있는 곳은 push 토큰을 가진 ④인데, 거기 도달하지 못한다.
- **얻는 것:** clone(`.git` 쓰기 필요)과 Codex(읽기 전용 요구)의 마운트 충돌이
  사라져 두 단계를 한 컨테이너에 넣을 수 있다. **컨테이너 5 → 4.**

**선례:** 격리가 3겹이던 시절 가장 안쪽(bwrap)이 바깥쪽(Pod 보안 컨텍스트) 때문에
못 떠서 파일 읽기조차 막혔고 그것이 `no_changes`의 원인이었다(PR #613). 예방 층을
겹치는 비용이 실재한다.

### AGENTS.md를 executor 전용 하네스 지침으로 교체한다

**현행은 버그다.** `AGENTS.md`(`CLAUDE.md`와 동일 내용)가 git 추적 파일이라 clone에
들어가고 Codex CLI가 작업 디렉터리에서 자동으로 읽는다. 내용이 executor 경계와
충돌한다 — "이슈를 먼저 발행하고 그 이슈로 브랜치 생성"(ref 생성 금지와 충돌),
"비자명한 변경은 `docs/specs/`에 계획 작성"(`docs/**` 금지와 충돌).

최악의 결과는 **"규칙상 못 하겠다"며 아무것도 안 하는 것**이고, 그러면 `no_changes`로
나와 실제 실패와 구분되지 않는다. 아직 관측되지 않은 이유는 모든 실행이 bwrap에서
먼저 죽어 그 뒤 행동을 보지 못했기 때문이다.

②가 clone 직후 Codex 실행 **전에** `AGENTS.md`를 executor 전용 지침으로 교체한다.

> **정정 (2026-08-09, #639).** "그 시점의 워크스페이스가 검증 baseline이므로 verifier가
> Codex의 변경으로 오인하지 않는다"는 **사실이 아니다.** verifier의 baseline은 그 시점의
> 워크스페이스가 아니라 봉인 커밋(`base_dev_sha`)이다 — `git status --porcelain`과
> `ls-files --others`로 변경을 수집하므로(`verifier._collect_changes`), 교체된
> `AGENTS.md`는 수정 파일로 잡혀 그대로 commit·push된다.
>
> → **교체는 Codex 실행 직전, 복원은 `finally`로 한다**
> (`codex_worker._harness_instructions`). 복원 실패는 하네스 파일을 저장소에 남기는 것과
> 같은 결과이므로 fail-closed로 끊는다.

**`AGENTS.md`는 저장소 루트라 현행 경로 정책에서 이미 허용 prefix에 없다** —
`src/` · `autoresearch/` · `tests/` · `tools/` · (조건부) `feature_repo/` 밖이므로
Codex가 고칠 수 없다. 하네스 지침을 지키는 별도 장치가 필요 없다.

### verifier의 정책 강제를 하네스로 대체한다

verifier가 하는 일은 둘인데 성격이 다르다.

| 역할 | 처리 |
|---|---|
| **정책 강제** — 경로 allowlist, credential 내용 검사, symlink·submodule 거부 | **하네스(AGENTS.md)로 대체** |
| **관측·핸드오프** — 변경 파일 목록, `no_changes` 판정, staged tree OID, content fingerprint, pytest 관측치 | **유지** |

정책 강제를 걷어내는 근거:

- **실행 중 보호가 아니다.** executor 이미지는 digest 고정이라 workspace의
  `agent_orchestration/**`를 고쳐도 **돌고 있는 executor는 바뀌지 않는다.** 이 정책이
  실제로 제한하는 것은 "무엇이 커밋으로 나가는가"뿐이다.
- **이미 층이 둘 있다.** MVP는 dev 자동 병합을 제외하고 `exp/N → main` Draft PR이므로
  사람이 diff를 보고 나서야 병합되며, 그 앞에 Claude 리뷰가 있다.
- **credential 내용 검사는 없는 것을 지킨다.** ②에는 GitHub·API 토큰이 마운트되지
  않고 GSA는 파일이 아니다. 복사할 대상이 존재하지 않는다.

관측·핸드오프는 규칙이 아니라 **사실 수집**이라 지시로 대체할 수 없다. `no_changes`는
"에이전트가 아무것도 하지 않았다"는 신호이고, tree OID·fingerprint는 finalizer가
commit 대상을 특정하는 데 쓴다.

**크기 상한은 유지한다** — 변경 50개 · 텍스트 diff 1 MiB · 파일 10 MiB ·
`.csv`/`.pkl`/`.parquet` 거부. 에이전트 통제가 아니라 **저장소 위생**이다. 실수로
학습 데이터가 git 히스토리에 들어가면 되돌리기 번거롭다.

## 결정 4 — ② worker 내부 계약

```
1. clone + 데이터셋 + AGENTS.md 교체
2. baseline 학습          seed별, dev 코드
3. Codex #1               가설 읽고 코드 수정
4. 검증                   아래 표
5. candidate 학습         seed별, candidate 코드
6. 측정                   기존 evaluate 경로 실행 → metrics.json
7. Codex #2 (resume)      metrics.json 읽고 report.md 작성
8. 산출물 확인            report.md 형식 검사
```

**Codex 호출은 2회이며 `codex exec resume`으로 세션을 잇는다.** 현행
`--ephemeral`(세션 파일 미보존)을 제거해야 resume이 가능하다. 맥락을 이어야
"왜 그렇게 고쳤는지"가 리포트에 들어간다. 최종 응답 형태는 `--output-schema`로
강제하고, 결과는 `-o/--output-last-message`로 파일에 받는다(현행 stdout tail
64 KiB 스크래핑을 대체한다).

**검증 정책:**

| 검사 | 처리 |
|---|---|
| 크기 상한 (변경 50개 / diff 1 MiB / 파일 10 MiB / 생성 데이터 확장자) | **차단** |
| git metadata digest 불일치 | **차단** |
| `git diff --check`, Ruff, `report.md` 형식 | **Codex에 되돌려 재시도** (최대 2회) |
| `no_changes` | **차단** — 에이전트가 아무것도 하지 않았다는 신호 |
| pytest | **관측치**로만 기록 (#615 유지) |

경로 allowlist와 credential 내용 검사는 이 표에 없다 — 하네스로 이관했다(§결정 3).

되돌려주는 대상을 구분하는 이유: 구문·린트 오류는 실수이고 재시도가 값을 하지만,
크기 상한 초과나 git metadata 변경은 다른 사건이다.

**`metrics.json` 무결성은 하네스 지침이 담당한다.** 코드 레벨 봉인을 겹치지 않는다.
숫자를 손대면 `report.md`·diff와 어긋나 리뷰가 잡는다. 게시 버킷의 IAM이
`objectCreator`(교체 불가)라 **게시된 사본은 부수적으로 write-once가 되지만, 그것을
전제로 설계하지는 않는다**(§확인 결과 1).

## 결정 5 — 산출물 계약

② → ④ 인계:

```
/workspace/repository/           검증 통과한 working tree   → ④가 commit·push
/workspace/training-output/      seed별 모델·테스트셋
/var/run/result/metrics.json     측정 결과
/var/run/result/report.md        Codex가 쓴 리포트
/var/run/verification-result/    검증 판정 + pytest 관측치
```

④가 GCS 실험별 경로에 게시한다. 버킷은
`gs://autoresearch-503903-autoresearch-dev-experiment-results`이며, `exp-job` GSA가
`objectCreator`만 가져 **교체가 IAM으로 차단된다**(§확인 결과 1). `metrics.json`과
`report.md`는 **판정 입력이므로 반드시** 게시한다. 모델·테스트셋은 재현·재측정을
위해 게시한다.

게시는 ④에서 한 번에 수행한다.

**`metrics.json`이 담는 것:**

- seed별 baseline·candidate의 ROC-AUC, LogLoss, Brier
- 참고 통계: paired delta 평균, 표준오차, 95% CI (판정이 아니라 수치)
- **`dataset_fingerprint`** — 두 조건이 같은 데이터를 썼는가
- **`split_hash`** — 두 조건이 같은 테스트셋을 썼는가
- 실험 좌표: `experiment_id`, `issue_number`, `base_dev_sha`, `candidate_sha`,
  `image_digest`, seed 목록

`split_hash`가 특히 중요하다. 테스트 분할 코드도 `src/**`라 candidate가 바꿀 수
있고, **분할이 달라지면 두 숫자는 애초에 비교 대상이 아니다.** 지표는 멀쩡해 보이는데
비교가 성립하지 않는 상태가 되며 리크보다 알아채기 어렵다. 게이트로 막는 것이
아니라 리뷰어가 판정하려면 알아야 하는 사실이므로 싣는다.

**`report.md`가 담는 것:** 가설, 무엇을 어떻게 바꿨는지, before/after 주 지표,
보조 지표, seed별 표, 데이터·분할 provenance, 에이전트 자신의 결론과 근거.

## 결정 6 — 상태 전이의 재해석

통계 게이트가 없으므로 `PASSED`/`FAILED`의 의미를 바꾼다.

| 상태 | 기존 의미 | 새 의미 |
|---|---|---|
| `PASSED` | 통계적으로 유의한 개선 | **실험이 완주하고 결과가 나왔다** |
| `FAILED` | 개선 없음 / 기각 | **실행이 실패해 결과가 없다** |
| `ERROR` | 판정 불가 | 그대로 |

**가설의 성패는 상태가 아니라 `report.md`가 서술한다.** 지표는 `metric_snapshot`에
그대로 실어 워크벤치에서 한눈에 보이게 한다.

`report-experiment-result`가 소비하는 `PairedExperimentResult` 스키마는
`outcome`·`decision_reason`·`reason_codes`·지표·`runs[]`만 실제로 읽고
`ConditionLineage` 등 격리 실행 모델 필드는 어디서도 읽지 않는다. 존재하지 않는
계보를 그럴듯하게 채우지 않기 위해, 이 경로 전용 결과 계약을 별도로 둔다.

## 범위 밖 (MVP 이후)

- Claude 리뷰어의 **거부권** — MVP에서는 리뷰 결과를 `report.md`에 병기만 하고
  상태를 바꾸지 않는다. 자동 기각은 리뷰어 판정이 숫자와 일관되게 움직이는지
  실적이 쌓인 뒤에 논한다.
- baseline 학습 **캐싱** — 같은 `base_dev_sha`로 실험을 두 번 돌려 seed별 값이
  일치하는지 확인한 뒤에 결정한다. 지금은 확인할 숫자 자체가 없다.
- `tests/**` 쓰기 권한 회수, 하네스 격리 디렉터리로의 산출물 계약 전환
- 피처 변경 실험(`feature_change_unsupported`) 해금
- `CREATED → ERROR` 전이

## 확인 결과 (2026-08-09)

**1. GSA 권한 — 유리한 쪽이다.** `autoresearch-dev-exp-job` GSA는 버킷
`autoresearch-503903-autoresearch-dev-experiment-results`에 정확히 두 역할을 가진다.

| 역할 | 의미 |
|---|---|
| `roles/storage.objectCreator` | 객체 **생성만**. 조회·삭제·**교체 불가** |
| `roles/storage.objectViewer` | 조회 |

프로젝트 레벨 IAM에는 이 GSA 바인딩이 없고 버킷 단위로만 부여돼 있다.
**`objectCreator`는 기존 객체 교체를 허용하지 않으므로 write-once가 IAM으로
강제된다.** 측정 직후 `metrics.json`을 이 버킷에 게시하면 Codex가 GSA로 GCS에
닿더라도 덮어쓸 수 없다. **부수적 성질로 기록해 두되 설계 전제로 삼지는 않는다.**

게시 대상 버킷을 이 버킷으로 확정한다.

**2. Codex CLI 0.146.0에 필요한 것이 전부 있다.** 실제 0.146.0 패키지를 받아
확인했다.

```
codex exec resume [OPTIONS] [SESSION_ID] [PROMPT]
  --last                          가장 최근 세션을 id 없이 이어받는다
  --output-schema <FILE>          최종 응답 형태를 JSON Schema로 강제
  -o, --output-last-message <FILE>  마지막 메시지를 파일로
  --ephemeral                     (제거 대상 — 켜면 세션이 저장되지 않아 resume 불가)
```

`--output-schema`와 `-o`는 `exec`와 `resume` 양쪽에 있다. **버전 업그레이드 없이
설계대로 구현 가능하다.**

> 구현 제약: 현행 `codex_worker`는 호출마다 `TemporaryDirectory`로 runtime
> `CODEX_HOME`을 만든다. 두 호출이 세션을 이어받으려면 **같은 `CODEX_HOME`을
> 공유**해야 한다.

**3. Claude 리뷰어는 Pod 밖에 둔다.** 확정.

**4. `ORCH_CODEX_TIMEOUT_SEC=6000`, `ORCH_ACTIVE_DEADLINE_SEC=60000`.** 확정하고
`.env.example`에 반영했다. 실제 클러스터 값은 infra 저장소 매니페스트에 있으므로
거기도 함께 바꾸어야 적용된다. config가 `codex_timeout_sec < active_deadline_sec`를
검증한다.

## 실측으로 확정된 제약 (2026-08-09, Stage 1 완주 후)

**5. 학습은 seed로 완전히 결정적이다.** 같은 `base_dev_sha`로 두 건(#634·#635)을
돌려 두 조건 × 3seed × 3지표 **18개 값이 소수점 아홉 자리까지 일치**했고
`test_set_sha256`도 6개 전부 같았다. Pod·노드·시각이 달라도 분할까지 재현된다.
→ paired delta를 코드 변경의 효과로 읽을 수 있고, **baseline 캐싱의 전제가 성립한다.**

**6. `train-model`은 학습과 서빙 ONNX 패키징을 한 덩어리로 수행한다.** 그래서
**트리 크기를 키우는 하이퍼파라미터 변경은 학습이 성공해도 실패할 수 있다.** #633에서
`num_leaves` 31→63이 학습·검증·모델 저장(Step 1~8)을 모두 통과한 뒤
`convert_lgbm_to_onnx`의 트리 파서 재귀에서 `RecursionError: maximum recursion depth
exceeded`로 죽었다. 로컬 재현으로 base(31) 성공·candidate(63) 실패를 확인했다.

이것은 **실험 공간에 숨은 제약**이다. 에이전트는 알 방법이 없고, 트리 크기 조정은
가장 자연스러운 첫 실험 아이디어 중 하나다. 두 갈래로 처리할 수 있다.

| 안 | 내용 |
|---|---|
| 분리 | ONNX 패키징 실패를 학습 실패와 분리한다. 실험의 목적은 지표 측정이고 ONNX는 서빙 산출물이다 — 패키징 실패로 측정을 통째로 잃는 것은 손해다 |
| 명시 | 분리하지 않는다면 **Stage 3 하네스 지침(AGENTS.md)에 제약을 적는다** |

**7. Codex 자격증명은 구조적으로 일회성이다.** `codex_worker._prepare_runtime_codex_home`이
`auth.json` 복사본을 `0400`으로 만들어 Codex가 갱신 토큰을 되쓰지 못하고, 쓰더라도
`TemporaryDirectory`라 사라진다. ChatGPT OAuth는 **refresh token을 쓸 때마다 교체**하므로
access token이 만료되는 순간 저장된 refresh token이 한 번 쓰이고 영구히 죽는다
(#632에서 `refresh_token_reused`로 관측). 구독제라 API key 전환은 불가능하다.

→ 복사본 모드를 `0600`으로 바꾸고 **갱신본을 Secret에 되쓴다.** 되쓰기는 같은
컨테이너에서 하고, Codex의 자격증명 접근 금지는 **하네스 지침이 담당한다** — 위
"verifier의 정책 강제를 하네스로 대체한다"와 같은 논리다(사용자 판단, 2026-08-09).
RBAC는 해당 Secret 하나에 `patch`만 준다. 착수 전에 모드를 풀었을 때 Codex가 실제로
`auth.json`을 갱신해 쓰는지부터 관측한다.

**8. 실패 원인이 로그에 남지 않는다** —
[#636](https://github.com/SKYAHO/Autoresearch/issues/636). `training._run`·
`measurement._run`이 subprocess 출력을 잡아둔 뒤 버려, 사유 코드만 남고 본문이 사라진다.
6·7 모두 이 때문에 진단 비용을 치렀다(#633은 로컬 재현 20분).

## MVP 구현에서 달라진 것 (2026-08-09, #639)

리포트를 **컨테이너 재구성보다 먼저** 넣기로 하면서(계획 순서 변경) 결정 3~5의 일부가
현행 8-container 구조에 맞게 달라졌다. 결정 자체를 뒤집은 것은 없고 착수 시점만 옮겼다.

| 계약 | MVP 구현 | 이유 |
|---|---|---|
| Codex #2가 ② worker 안에서 `resume`으로 세션을 잇는다(결정 4) | `candidate-finalizer` 안에서 **새 호출**로 돈다. 입력은 `metrics.json` + candidate diff | `report.md`는 git 커밋 대상이 아니라 GCS 게시 산출물이라(결정 5) push 뒤에 와도 된다. 세션 유지는 토큰 절약 최적화이지 기능이 아니다 |
| 산출물 경로 `/var/run/result/`(결정 5) | `<workspace>/result/` — clone 밖, `metrics.json`과 같은 자리 | 게시가 같은 container에서 일어나 volume 핸드오프가 필요 없다. Stage 1의 `metrics.json` 경로를 그대로 쓴다 |
| ② worker에는 credential이 없다(결정 3) | Codex #2가 도는 `candidate-finalizer`에는 **push token·API token이 함께 있다** | 결정 3의 "미결" 블록이 이미 지적한 모순의 같은 얼굴이다. 코드로 막지 않고 하네스 지침이 담당한다 — 컨테이너를 갈라 없애는 것은 Stage 2의 몫이다 |
| `report.md` 형식 검사 실패 시 되돌려 재시도(결정 4) | 빠진 절을 **로그로만** 남기고 리포트는 게시한다 | 형식이 어긋난 리포트가 리포트 없음보다 낫다. 재시도는 Codex 한 번을 더 태우고, 절 이름 하나 때문에 그럴 값이 없다 |
| 게시는 ④에서 **한 번에** 수행한다(결정 5) | **두 번에 나눈다** — 채점 직후 `metrics.json`·학습 산출물, Codex #2 뒤 `report.md` | 아래 참조 |

**Codex #2 실패는 어떤 경로로도 위로 올리지 않는다.** 리포트가 없다고 `metrics.json`
게시와 API 보고까지 잃으면 측정한 숫자마저 사라진다. 리포트는 최종 산출물이지만 숫자보다
뒤에 오고, 둘 중 하나만 남길 수 있다면 숫자다.

**그래서 게시도 숫자가 먼저다.** 예외를 잡는 것만으로는 부족하다 — Codex #2는 최대
`ORCH_CODEX_TIMEOUT_SEC`(6000초) 동안 돌고, 그 사이 `activeDeadlineSeconds`나 OOM으로
container가 죽으면 잡을 예외 자체가 없다. 그 시점에는 push와 `RUNNING → EVALUATING`이
이미 끝났으므로 실험은 `reconcile_failed_jobs`가 ERROR로 회수하고, 측정한 숫자는
emptyDir과 함께 사라진다. 채점 직후에 게시하면 이 창이 닫힌다.

부수 효과로 **결정 4의 "`metrics.json` 무결성" 문제도 함께 닫힌다.** 게시된 사본은
`objectCreator`(교체 불가)라 write-once이므로, 같은 디렉터리에서 도는 Codex #2가 로컬
파일을 고치더라도 게시된 숫자와 API에 보고된 요약은 그대로다. 코드 레벨 봉인을 새로
얹은 것이 아니라 **순서만 바꾼 결과**다.

### 하네스 지침이 닿지 않는 곳 (2026-08-09 리뷰에서 드러남)

"Codex의 자격 증명 접근 금지는 하네스 지침이 담당한다"는 서술은 **Codex #1에만**
적용됐다. 하네스는 clone 루트 `AGENTS.md`로 심기는데 Codex #2는 clone 밖
(`<workspace>/result/`)에서 돌아 그 파일을 한 줄도 보지 못한다. 그런데 자격 증명이
실제로 마운트된 container는 Codex #2가 도는 `candidate-finalizer`다.

두 가지로 메웠다.

1. **리포트 지시문에 credential 규칙을 넣는다**(`build_report_prompt`) — 지침이 닿지
   않는 호출에는 지시문이 지침을 대신한다.
2. **산출물이 regular file인지 코드로 확인한다.** 이것만은 지시에 맡기지 않는다 —
   `report.md`를 토큰 파일을 가리키는 symlink로 만들면 `read_text`·`is_file()`·
   `upload_from_filename`이 모두 링크를 따라가 토큰이 그대로 GCS에 올라간다. 같은 이유로
   하네스 파일 교체·복원도 링크를 지우고 `O_CREAT | O_EXCL`로만 쓴다 — 실행 전 검사만으로는
   실행 도중 심긴 링크를 막지 못하고, 복원이 그 링크 대상을 덮어쓴다.

경계 판단: **에이전트의 판단에 맡길 수 있는 것은 지시문으로, 파일 시스템이 조용히
따라가 버리는 것은 코드로 막는다.**

## 검증

- [x] 실험 1건이 `metrics.json`을 GCS에 남긴 채 완주한다 — **#634 (33분 33초)**
- [x] `metric_summary`가 `null`이 아니다 — 전문↔요약 9항목 일치
- [x] 같은 `base_dev_sha`로 2건을 돌려 baseline seed별 값이 일치하는지 관측한다 — 일치
- [x] `uv run python -m pytest`(2516 passed), `uv run --no-sync ruff check ...`
- [ ] `report.md`가 GCS에 남는다 — Stage 3 범위
