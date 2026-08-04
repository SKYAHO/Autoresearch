# 가설 수신부터 `[AR]` 이슈 발행까지 (#516)

- **상태**: Proposed
- **날짜**: 2026-08-04
- **이슈**: #516
- **선행 계약**: `.github/ISSUE_TEMPLATE/auto_research.yml`(필드 정본),
  `tools/auto_research_issue_branch.py`(파싱 정본),
  `src/pipeline/experiment_evaluation.py`(`POLICY_SEEDS` 정본),
  `docs/specs/2026-07-30-codex-oauth-runner-isolation.md`(깨뜨리면 안 되는 보안 속성)

## 목적

`auto-experiment` label 하나로 exp 브랜치가 생기는 데까지는 자동화됐다(#507). 그러나
**그 이슈를 만드는 주체가 저장소에 없다.** `[AR]` 이슈는 저장소 역사상 0건이고,
승격 워크플로 2종의 실행 이력도 0건이다.

`agent_orchestration`에는 이미 가설을 받는 계약이 있다 — `POST /experiments`의
`ExperimentCreate.hypothesis`(1~8192자). 그러나 `create_experiment()`는 `Experiment`
행과 최초 `CREATED` event를 만들고 끝나며, 이슈를 발행하지도 lineage를 남기지도 않는다.

이 문서는 그 빈 구간 — **가설 수신 → 본문 생성 → 이슈 발행 → lineage 기록** — 의
계약을 정의한다.

## 범위

**포함**: 위 네 구간과 그에 필요한 스키마·설정·이미지 변경.

**범위 밖**

| 항목 | 소유 |
| --- | --- |
| exp 브랜치에서 candidate SHA를 만드는 실험 실행기 | #492 |
| 판정 엔진 단일화, `required_seeds`를 선언 값으로 받는 변경 | #493 |
| 실험 종료 후 `### 결과` 섹션을 채우는 결과 보고 양식 | #494 |
| Streamlit UI | #484 |
| 클러스터 배포(Deployment/Service/Ingress)와 시크릿 | `Autoresearch-infra` |

## 결정 1 — 발행 주체는 Agent Orchestration API Pod다

추론과 쓰기를 서로 다른 경계에 둔다.

```
[UI/호출자] ──POST──▶ [API Pod] ──프롬프트──▶ [Runner Pod] ──▶ codex exec
                          │                                   (본문 값 JSON)
                          ├─ 서버가 heading과 결합해 본문 조립
                          ├─ Experiment.issue_body 저장 (commit)   ← 발행 전
                          ├─ gh issue create + auto-experiment label
                          └─ Experiment.issue_number / issue_branch 기록
```

**Codex는 계속 read-only·ephemeral이며 텍스트만 제안한다.** 파일을 쓰거나 GitHub을
부르는 주체는 신뢰된 저장소 코드다. #492가 정한 "추론 경계와 쓰기 경계의 분리"를
그대로 따른다.

### 기각한 대안

**GitHub Actions가 발행하는 안.** 두 가지가 막는다. Actions runner는 GKE 내부 Runner
Pod에 도달할 수 없어 본문을 만들 수 없고, Codex OAuth를 Actions로 옮기면 격리 spec
위반이다. 또한 `repository_dispatch`를 보내려면 `contents: write`가 필요해 이 안의
`issues: write`보다 **권한이 넓어진다.**

**별도 publisher worker Deployment.** 격리는 낫지만 새 Deployment·서비스 계정·시크릿·
관측을 인접 저장소에 전부 만들어야 한다. 사람이 UI에서 가끔 누르는 저빈도 경로에
과하다. 필요해지면 아래 §구성 요소의 `github_issues.py`만 옮기면 된다.

### 격리 spec과 충돌하지 않는 근거

`docs/specs/2026-07-30-codex-oauth-runner-isolation.md`의 속성 2는 **runner**에 소스
저장소·자격 증명을 두지 않는 것이다. 이 설계는 **Runner Pod에 아무것도 추가하지
않는다.** API Pod가 갖는 것은 `issues: write` 하나이며, 이 자격으로는 코드를 쓸 수 없다.
브랜치를 만드는 것은 GitHub Actions가 자기 `GITHUB_TOKEN`으로 하는 별개의 일이다.

#492가 API Pod에 두지 말라고 한 것은 **저장소 checkout·push 권한**이며 여기에 해당하지
않는다. 수명도 다르다 — 발행은 추론 1회와 CLI 호출 1~2회로 끝나 HTTP 요청 수명에
들어가며, #492가 문제 삼은 "수 시간 수명"은 실험 실행기의 사정이다.

## 결정 2 — 필드 소유권을 셋으로 나눈다

Issue Form 필수 18개를 다음과 같이 나눈다.

| 주체 | 개수 | 필드 |
| --- | --- | --- |
| **LLM** | 8 | 연구 가설, 변경할 피처 · 모델, 주 지표 이름, 주 지표 방향, 최소 주 지표 개선폭, Guardrail 지표 이름/방향/최대 악화폭 |
| **사용자** | 1 | 허용 범위 |
| **서버** | 9 | 비교 대상, 데이터셋 스냅샷, 랜덤 시드 목록, Split 시드, Test 비율, Validation 비율, 학습 설정 참조, 대상 데이터 · 기간, 스냅샷 재사용 |

선택 섹션 `보조 관측 지표`는 LLM이 채우거나 heading을 생략한다.
`결과 (에이전트가 채웁니다)`는 실험 종료 후이므로 #494 소유다.

**`허용 범위`를 사용자가 갖는 이유는 안전이다.** 이 체크박스는 "prod 모델 계약
(`src/features/model_contract.py`) 수정 허용", "Feast 정의 수정 허용", "champion 승격
검토"를 정한다. 실행기가 수정할 수 있는 파일 범위의 정본이므로, **에이전트가 자기
권한을 스스로 넓히게 두지 않는다.**

**서버가 실행 설정 9개를 갖는 이유는 정확성이다.** 이 묶음에는 정확 문자열 옵션
5개(`비교 대상` 3, `스냅샷 재사용` 2)와 정책값이 모두 들어 있다. 창작 대상이 아니라
참조 대상이다.

## 결정 3 — 시드는 `42..71` 고정이다

`랜덤 시드 목록`에 `src/pipeline/experiment_evaluation.py:46`의 `POLICY_SEEDS`를
**참조해** 오름차순으로 쓴다.

```
42, 43, 44, ... , 71
```

`src/pipeline/paired_experiment.py:266-272`가 양방향 차집합으로 검사한다.

```python
seeds = {run.seed for run in request.runs}
if sorted(set(POLICY_SEEDS) - seeds):
    reasons.append(PairedExperimentReason.MISSING_PAIRED_RUN)
if sorted(seeds - set(POLICY_SEEDS)):
    reasons.append(PairedExperimentReason.SEED_POLICY_MISMATCH)
```

통과하는 집합은 정확히 `{42, ..., 71}` 하나뿐이다. **개수나 순서가 아니라 값이
고정이다** — 랜덤 30개를 정렬해도 통과하지 못한다. 순서는
`paired_experiment`가 정규화하지만(`tests/test_paired_experiment.py:545-549`),
이슈 본문·`reproducibility_id`·판정 입력의 표현을 일치시키기 위해 오름차순으로 고정한다.

Issue Form 기본값은 `42, 43, 44`(`.github/ISSUE_TEMPLATE/auto_research.yml:154`)이며
파서는 개수를 검사하지 않는다. 이 값으로 발행하면 판정이 **항상**
`comparison_failed`로 끝난다(#493 시나리오 2). 실패 방향은 안전하지만 폐루프가 도는
것처럼 보이면서 아무 결론도 나오지 않는다.

**하드코딩하지 않는다.** `POLICY_SEEDS`를 참조하고, 테스트가 동일성을 고정한다.
정책이 바뀌면 한 곳만 바뀐다.

실험마다 랜덤 30개를 뽑는 안은 재현성을 해치지 않고(발행 시점에 `reproducibility_id`로
봉인되므로 cherry-picking 불가) 시드 다양성이 늘어 타당하지만, `required_seeds`를 선언
값으로 받도록 판정 엔진을 고쳐야 한다. 그 두 파일은 #493 소유이므로 이 spec에서 하지
않고 **#493에 요구로 남긴다.** 이후 서버의 시드 생성 함수만 교체하면 된다.

## 결정 4 — LLM은 heading을 타이핑하지 않는다

LLM은 자기 담당 8필드를 **JSON으로만** 반환하고, 서버가 heading과 결합해 최종 본문을
조립한다.

```
LLM 출력   {"hypothesis": "...", "primary_metric_name": "roc_auc", ...}
              ↓
서버 조립   <!-- experiment-id: <uuid> -->
            ### 연구 가설
            ...
```

heading 20개 문자열, U+00B7 가운뎃점, 정확 문자열 옵션, 체크박스 label을 LLM이 만들 일이
없어진다. **fail-closed 거부 사유의 대부분이 구조적으로 사라진다.**

프롬프트 고정분은 약 2,500자다(지시문 + LLM 담당 8필드 규칙 + 축약 예시). `/chat`의
8192자 상한은 그 endpoint의 입력 정책일 뿐 Runner의 `GenerateRequest.prompt`에는 제약이
없으므로, 서버가 조립하는 프롬프트는 이 상한을 지나지 않는다.

**작성 가이드 파일은 지우거나 줄이지 않는다.** `docs/guides/auto-research-issue-authoring.md`
(#490/PR #501)는 사람과 터미널 에이전트를 위한 문서이며 `gh issue create` 절차와 복구
절차가 그 소비자에게는 여전히 참이다. 프롬프트는 그 파일을 넣지 않고 별도로 조립한다.

## 결정 5 — 발행 수단은 `gh` CLI다

`httpx`로 Issues API를 직접 부르는 대신 `gh issue create` 서브프로세스를 쓴다. 사람·
터미널 에이전트·서버가 **같은 명령**을 쓰게 되어 작성 가이드가 모든 경로에서 참이 된다.

대가는 오류가 HTTP 상태 코드가 아니라 exit code와 stderr 문자열로 온다는 점이다.
다음으로 대응한다.

- `api.Dockerfile`에 `gh`를 **버전 고정**해 설치한다. 고정하지 않으면 stderr 문자열 기반
  분류가 버전 변경으로 조용히 깨진다.
- **요청 안에서는 어떤 실패도 자동 재시도하지 않는다.** `gh issue create`는 멱등이
  아니므로, 성공했는데 응답만 소실된 경우를 요청 안에서 구분할 수 없다. 재시도는
  호출자가 다시 부를 때 §결정 7의 멱등성 3중 방어가 처리한다. 오류 분류는 호출자에게
  사유를 알려주기 위한 것이지 재시도 판단을 위한 것이 아니다.

인증은 `GH_TOKEN` 환경변수로 충분하다 — 설정 파일도 `gh auth login`도 필요 없다.
실행 환경은 `agent_orchestration/codex.py`의 화이트리스트 방식을 재사용하고,
컨테이너가 non-root `appuser`이므로 `GH_CONFIG_DIR`을 요청별 임시 디렉터리로 지정한다.

성공 시 stdout에 이슈 URL 한 줄이 나온다(`gh issue create`에 `--json` 플래그가 없음,
gh 2.97.0 확인). 여기서 이슈 번호를 뽑되 **저장소 경로까지 검증**해, 예상과 다른
저장소 URL이면 실패로 처리한다.

**LLM은 이 명령을 실행할 수 없다.** `_codex_environment()`가 넘기는 것은 `CODEX_HOME`,
`HOME`, `TMPDIR`, `XDG_CACHE_HOME`, `XDG_STATE_HOME`, `PATH`뿐이라 `gh`가 PATH에 있어도
인증이 없고, `--sandbox read-only`이며 작업 디렉터리에 저장소가 없다.

## 결정 6 — 발행 전 파서 검증을 하지 않는다

`tools/auto_research_issue_branch.py`를 런타임에서 호출하지 않는다. `tools/`는 API
이미지에 들어 있지 않고(`api.Dockerfile`은 `agent_orchestration/`만 복사한다), 넣으면
서비스 코드가 스크립트 계층에 의존하게 된다.

잘못된 본문은 실제 이슈로 열리고, 워크플로의 검증 step이 브랜치 생성 step보다 먼저
실행되므로 **브랜치는 생기지 않고 실패한 run만 남는다.** 복구는 본문 수정 후 label
재부여다.

**대신 테스트가 조립 결과를 실제 파서로 검증한다.** 런타임은 `tools/`를 import하지
않지만 CI는 저장소 전체를 본다. 이 방식으로 런타임 의존성을 늘리지 않고 검증만 얻는다.

## 결정 7 — 발행은 별도 endpoint이며 멱등이다

```
POST /experiments/{experiment_id}/issue
```

`POST /experiments`와 분리한다. 후자는 순수 DB 쓰기이고 즉시 응답하는 반면, 발행은 LLM
호출을 포함해 수십 초가 걸리고 실패 지점이 다르다. 호출자는 두 번 호출한다.

요청은 `allowed_scope`(`prod_model_contract` / `feast_definition` / `promotion` 중
0개 이상)와 `regenerate`(기본 `false`)를 받는다. 응답은 `issue_number`, `issue_url`,
`issue_branch`다.

### 본문 생성과 발행을 분리한다

한 요청 안에서 처리하되 **본문을 DB에 먼저 커밋한 뒤 발행한다.**

```
① issue_body가 이미 있나?
     없음 → LLM 호출 → 조립 → Experiment.issue_body 저장 (commit)
     있음 → 그대로 사용 (LLM 재호출 없음)
② issue_number가 이미 있나?
     있음 → 기존 값 반환
     없음 → 저장된 본문으로 gh issue create → issue_number / issue_branch 기록
```

**본문을 저장하지 않으면 재시도가 다른 실험을 만든다.** LLM은 비결정적이므로 `gh`
실패 후 재호출하면 지표 임계나 guardrail 설정이 달라질 수 있고, 파서가 그 값들로
계산하는 `criteria_id`·`reproducibility_id`도 함께 달라진다. 같은 가설을 재시도했을
뿐인데 실험 정의가 바뀌며, 호출자는 그 사실을 알 방법이 없다.

저장하면 재시도가 **같은 본문으로** 발행되어 결정론적이고, 추론 비용도 아낀다. 발행에
실패했을 때 "무엇을 발행하려 했는지"가 DB에 남아 원인을 볼 수 있다는 부수 효과도 있다.

**`regenerate`가 필요한 이유.** 본문이 저장되면 파서를 통과하지 못하는 본문도 고착되어
재시도가 같은 실패를 반복한다. `regenerate: true`면 저장된 `issue_body`를 버리고 다시
만든다. 단 **`issue_number`가 이미 있으면 `regenerate`를 무시한다** — 이미 발행된
이슈의 본문을 바꾸는 것은 이 endpoint의 책임이 아니다.

### 멱등성 3중 방어

| 순서 | 검사 | 결과 |
| --- | --- | --- |
| 1 | `Experiment.issue_number`가 이미 있나 | 발행하지 않고 기존 값 반환 (200) |
| 2 | 일일 발행 상한 초과했나 | 429 |
| 3 | `gh` 성공 후 DB 쓰기 실패 | 본문 marker로 GitHub에서 기존 이슈 조회 |

3번이 가장 까다롭다. `gh issue create`에는 멱등성이 없으므로, 이슈는 만들어졌는데 응답이
소실되면 재시도가 중복 이슈를 만든다. 본문 첫 줄에 다음 marker를 넣어 재시도 시
조회한다.

```
<!-- experiment-id: 3f2a1c9d-8b7e-4a1f-9c2d-5e6f7a8b9c0d -->
```

**이 marker가 파서를 깨지 않고 봉인 식별자도 바꾸지 않음을 실측 확인했다.** `_parse_sections`가
첫 `###` heading 앞의 내용을 무시하므로, marker 유무와 무관하게 `criteria_id`와
`reproducibility_id`가 동일했다.

## 결정 8 — 가설 상한은 기존 8192자를 그대로 둔다

`ExperimentCreate.hypothesis`의 `max_length=8192`는 `/chat`의 `ChatRequest.prompt`에서
물려받은 값이다. `tests/test_experiment_service.py:575`가 그 상속을 명시한다
("`/chat`의 8192자 상한과 동일하게"). 가설 자체에 대한 근거는 없다.

검토했으나 **좁히지 않는다.**

- 실제 가설은 한 줄에서 한 문단이다. fixture의 예시는 19자이고, 8192자는 실질적으로
  걸리지 않는 방어선이다. 좁혀서 얻는 것이 없다.
- 발행 endpoint는 가설을 요청으로 받지 않고 **DB에서 읽는다.** 여기에 더 좁은 상한을
  두면 생성에 성공한 실험이 발행 단계에서 거부되어, 사용자가 되돌릴 수 없는 상태가
  된다.
- `ExperimentCreate` 쪽을 좁히는 것은 이 spec의 범위 밖인 기존 계약 변경이다.

프롬프트 길이 예측 가능성은 이 상한이 아니라 §결정 4(서버가 프롬프트를 조립하고 Runner에
길이 제약이 없음)로 이미 확보된다.

## 구성 요소

```
agent_orchestration/app/experiments/
├── issue_authoring.py 신규 · 순수 함수. 프롬프트 조립, LLM JSON + 서버 값 → 본문 문자열
├── github_issues.py   신규 · gh CLI 경계. 서브프로세스·환경 화이트리스트·timeout·오류 분류
├── service.py         기존 · 생성→저장→발행 2단계 오케스트레이션, 멱등성·상한 검사
├── router.py          기존 · endpoint 추가
├── models.py          기존 · issue_body / issue_number / issue_branch 컬럼
└── schemas.py         기존 · 요청·응답 계약
migrations/versions/0002_experiment_issue_lineage.py   신규
```

`issue_authoring.py`를 순수 함수로 유지하는 것이 중요하다 — **본문 조립을 LLM·GitHub 없이
단독으로 테스트**할 수 있어야 §테스트의 파서 대조가 성립한다.

`github_issues.py`는 `llm.py`가 LLM 바깥 경계를 맡는 것과 같은 위치다. 테스트에서
GitHub 호출만 갈아 끼울 수 있고, 나중에 별도 worker로 옮길 때도 이 모듈만 이동한다.

### 스키마

`Experiment`에 세 컬럼을 1급으로 추가한다.

| 컬럼 | 타입 | 채워지는 시점 |
| --- | --- | --- |
| `issue_body` | `Text` | 본문 생성 직후 (발행 **전**) |
| `issue_number` | `Integer` | 발행 성공 후 |
| `issue_branch` | `String` | 발행 성공 후 |

- **`issue_body`가 발행보다 먼저 커밋되는 것이 핵심이다.** §결정 7의 재시도 결정성이
  여기에 의존한다. 길이 제약을 두지 않는다 — `Text`이고, 조립된 본문은 fixture 기준
  약 1,300자다.
- `ExperimentMetadata`로 대신할 수 없다. `key` `String(64)` / `value` `Text`라 형식이
  보장되지 않고, 더 큰 문제로 **metadata는 `create_experiment()`가 생성 시 1회만
  기록하며 갱신 endpoint가 없다.** 세 값 모두 생성 이후에 확정된다.
- 기존 revision이 `0001_experiment_tables` 하나뿐이므로
  `down_revision = "0001_experiment_tables"`로 연결하고 `upgrade()`/`downgrade()`를
  대칭으로 작성한다. 세 컬럼을 **한 revision**에 넣는다.
- 기존 행이 있으므로 **전부 nullable**로 추가한다.
- `issue_number`에 index를 두되 **unique 제약은 두지 않는다** — 이슈 1건이 실험 N건을
  가질 수 있다(대시보드 #338이 주 소비처다).
- `ExperimentResponse`에는 `issue_number` / `issue_branch`만 노출한다. `issue_body`는
  이슈 본문으로 이미 공개되어 있고 목록 응답을 크게 만들 뿐이다.
- `models.py` 모듈 docstring이 migration과의 동일성을 단언하므로 같은 커밋에서 갱신한다.

## 실패 처리

| 실패 | 처리 | 재호출 시 |
| --- | --- | --- |
| LLM 호출 실패·timeout | 502. `Experiment`는 `CREATED` 유지 | 본문을 다시 생성 |
| LLM 응답이 JSON이 아님 | 1회 재시도 후 실패 (LLM 호출은 부작용이 없어 재시도가 안전하다) | 본문을 다시 생성 |
| LLM이 낸 값이 형식 위반 | 조립 단계에서 거부, 실패 | 본문을 다시 생성 |
| `issue_body` 저장 실패 | 실패 | 본문을 다시 생성 |
| `gh` 실패 | 사유를 분류해 알리되 **요청 안에서 재시도하지 않는다** | **저장된 같은 본문으로 발행** |
| `gh` timeout | 프로세스 그룹 회수 후 실패 (`codex.py` 패턴) | **저장된 같은 본문으로 발행** |
| 발행 성공 후 DB 쓰기 실패 | 실패 | marker 조회로 기존 이슈를 찾아 복구 |
| 저장된 본문이 파서를 통과하지 못함 | 워크플로가 fail-closed, 브랜치 없음 | `regenerate: true`로 다시 생성 |

`issue_body` 커밋을 경계로 **앞쪽 실패는 재생성, 뒤쪽 실패는 재발행**으로 갈린다.

상태 기계는 건드리지 않는다. 발행 여부는 `issue_number`의 `NULL` 여부로 표현하며,
`CREATED → RUNNING` 전이는 실험 실행기(#492)의 몫이다.

## 설정

| 변수 | 용도 |
| --- | --- |
| `ORCH_GITHUB_TOKEN` | `gh`에 `GH_TOKEN`으로 전달. `issues: write` 전용 |
| `ORCH_GITHUB_REPOSITORY` | `owner/repo`. 발행 대상 고정과 URL 검증 |
| `ORCH_ISSUE_DAILY_LIMIT` | 일일 발행 상한 |
| `ORCH_GH_TIMEOUT_SEC` | `gh` 서브프로세스 상한 |

`load_settings()`가 기존 토큰들과 같은 방식으로 읽고, `.env.example`에 기재한다.
필수 환경 변수를 도입하므로 같은 PR에서 `README.md`와
`.claude/docs/agent-project-reference.md`를 갱신한다.

## 테스트

- **서버가 조립한 본문이 실제 `parse_issue_input`을 통과한다.** LLM 담당 8필드를
  guardrail 유무·지표 방향 등으로 바꿔가며 검증한다. 결정 6의 위험을 CI가 대신 막는다.
- **프롬프트 규칙과 파서 상수가 어긋나면 실패한다.** `_COMPARISONS`, `_SNAPSHOT_REUSE`,
  `_SCOPE_LABELS`, `_METRIC_DIRECTIONS`, `_HEADING_NAMES` 대조.
- **서버가 쓰는 시드가 `POLICY_SEEDS`와 같다.**
- **본문 marker가 파서와 봉인 식별자에 영향을 주지 않는다.**
- **`gh` 실패 후 재호출이 LLM을 다시 부르지 않고 저장된 본문으로 발행한다.** LLM 스텁의
  호출 횟수와 두 번의 발행 본문이 같은지로 고정한다 — §결정 7의 재시도 결정성이
  이 테스트에 달려 있다.
- **`regenerate: true`가 본문을 다시 만들고, `issue_number`가 있으면 무시된다.**
- `gh` 경계를 스텁으로 대체해 멱등성 3중 방어·상한·실패 분류를 검증한다.
- Alembic `upgrade()`/`downgrade()` 대칭.

## 완료 조건

- 로컬에서 가설을 보내면 실제 `[AR]` 이슈가 열리고, 워크플로가 `dev` tip을 봉인해
  `exp/<번호>-<slug>` 브랜치를 만들고 marker 코멘트를 남기는 것을 **1회 실증**한다.
- 같은 실험에 발행을 두 번 요청해도 이슈가 하나만 생긴다.
- 발행에 실패한 뒤 재호출하면 LLM을 다시 부르지 않고 저장된 본문으로 발행한다.
- 발행된 본문의 `랜덤 시드 목록`이 `42..71`이다.
- `uv run python -m pytest`와
  `uv run --no-sync ruff check agent_orchestration autoresearch tests tools` 통과.

검수 발행물은 **close하되 exp 브랜치는 남긴다.** 브랜치만 지우면 fail-closed되고,
marker까지 지우면 다른 기준선으로 조용히 재생성되어 원래 `base_dev_sha`를 인용한 곳과
아무 실패 없이 어긋난다.

## 이 저장소 밖 의존성

| 항목 | 소유 | 없으면 |
| --- | --- | --- |
| `agent_orchestration` Deployment·Service·Ingress | `Autoresearch-infra` | 이미지는 `release.yml:404`가 GAR에 푸시하지만 클러스터에서 실행되지 않아 브라우저로 도달할 수 없다 |
| `issues: write` 자격 발급·보관 | `Autoresearch-infra` | 배포 환경에서 발행 불가 (로컬은 개인 `gh` 인증으로 가능) |
| Alembic 마이그레이션 실행 | `Autoresearch-infra` | 새 컬럼이 반영되지 않는다. `api.Dockerfile` 주석이 이 책임 경계를 명시한다 |
| Streamlit UI | #484 | 사람이 화면에서 입력할 수 없다 (curl·스크립트는 가능) |

## 미결 항목

- **이슈 작성자 정체성.** PAT를 쓰면 발행된 이슈의 작성자가 개인 계정이 되고, GitHub
  App을 쓰면 봇이 된다. 자격 발급이 `Autoresearch-infra` 소유이므로 그쪽과 함께 정한다.
- **#484의 실제 상태.** 2026-08-04에 COMPLETED로 닫혔으나 저장소에 Streamlit 코드가
  없고 관련 브랜치·PR도 없다. 마지막 코멘트는 "구현을 재개합니다"로 끝난다. 담당자
  확인이 필요하다.
