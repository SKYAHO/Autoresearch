# 가설 수신부터 `[AR]` 이슈 발행까지 (#516)

- **상태**: Accepted (#536에서 부분 개정)
- **날짜**: 2026-08-04 (개정 2026-08-05)
- **이슈**: #516, #536
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

## 개정 (#536, 2026-08-05) — LLM을 발행 경로에서 제거했다

**본문 값을 LLM이 창작하지 않는다. 호출자가 사전등록 필드를 제출한다.**

이 시스템의 사용자는 ML 리서처이며, 예측 모델링 사전등록 표준
([arXiv 2311.18807](https://arxiv.org/abs/2311.18807))에서 **성공 기준을 실험 전에
연구자가 선언하는 것**이 제도의 핵심이다. 에이전트가 임계값을 창작하면 HARKing과
임계값 사후조정을 막는다는 성질이 사라진다. 반대로 데이터·split·시드(표준의
A.4/A.5/B.2)는 실험 간 비교가 성립하도록 서버가 계속 고정한다.

Issue Form 18필드는 이 표준을 이미 거의 1:1로 구현하고 있어 필드 집합을 새로
설계하지 않았다. 표준 대비 빠져 있던 **선행 연구 참조**만 선택 섹션으로 추가했다 —
선택이므로 `criteria_id`·`reproducibility_id` 계산에 들어가지 않아 기존 실험의
봉인값이 바뀌지 않는다.

아래 결정 중 다음이 개정됐다.

| 결정 | 개정 내용 |
| --- | --- |
| §결정 2 | LLM 8필드가 **호출자 소유**로 옮겨졌다. 서버 소유 9필드는 그대로다 |
| §결정 4 | LLM이 heading을 타이핑하지 않는다는 문제 자체가 사라졌다. `build_prompt()`·`parse_llm_fields()`를 삭제했다 |
| §결정 7 | `regenerate` 플래그를 삭제했다. 호출자가 값을 주므로 "LLM 비결정성 때문에 재생성이 필요한" 상황이 없다 |
| §실패 처리 | LLM 관련 실패 4종(502, JSON 아님, 재시도, 형식 위반)이 사라졌다. 형식 위반은 요청 검증에서 **422**로 끊기며 그 시점에 이슈는 열리지 않았다 |

**§결정 1(발행 주체는 API Pod), §결정 3(시드 42..71), §결정 5(`gh` CLI),
§결정 6(발행 전 파서 검증 안 함), §결정 7의 멱등성 3중 방어와 `issue_body` 선커밋은
그대로다.** `issue_body`를 계속 발행 전에 커밋하는 이유는 LLM 비결정성이 아니라
`대상 데이터 · 기간`이 발행 시점 KST 날짜로 계산되기 때문이다 — 저장하지 않으면
날짜가 바뀐 재발행이 다른 본문을 쓴다.

입력 표면은 Streamlit 폼이다. MCP 서버는 만들지 않았다 — 같은 API를 부르는 창구가
하나 더 생기는 것뿐이라 필요해지면 나중에 얹는다.

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

```
[UI/호출자] ──POST fields──▶ [API Pod]
                                 ├─ 요청 필드 검증 (위반 시 422, 이슈 없음)
                                 ├─ 서버가 heading·정책값과 결합해 본문 조립
                                 ├─ Experiment.issue_body 저장 (commit)   ← 발행 전
                                 ├─ gh issue create + auto-experiment label
                                 └─ Experiment.issue_number / issue_branch 기록
```

> 원안은 여기서 Runner Pod의 Codex를 불러 본문 값을 받았다. #536에서 그 구간을
> 제거했으므로 **이 경로는 Runner Pod를 호출하지 않는다.** Runner는 `/chat`이 계속
> 쓰며, 이 spec이 지키려던 "추론 경계와 쓰기 경계의 분리"(#492)는 추론이 사라져
> 자동으로 성립한다.

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
않는다.** API Pod는 이슈 발행용 `issues: write` 자격과 `heads/dev` 기준 SHA 조회용
Contents read 전용 GitHub App만 가지며, 어느 자격으로도 코드를 쓸 수 없다. branch-writer
App private key는 executor의 token-minter initContainer에만 mount되고, executor 본
container는 짧은 수명의 token 파일로 DB에 봉인된 SHA의 ref만 생성한다.

#492가 API Pod에 두지 말라고 한 것은 **저장소 checkout·push 권한**이며 여기에 해당하지
않는다. 수명도 다르다 — 발행은 추론 1회와 CLI 호출 1~2회로 끝나 HTTP 요청 수명에
들어가며, #492가 문제 삼은 "수 시간 수명"은 실험 실행기의 사정이다.

## 결정 2 — 필드 소유권을 둘로 나눈다

Issue Form 필수 18개를 다음과 같이 나눈다.

| 주체 | 개수 | 필드 |
| --- | --- | --- |
| **호출자** | 9 | 연구 가설, 변경할 피처 · 모델, 주 지표 이름, 주 지표 방향, 최소 주 지표 개선폭, Guardrail 지표 이름/방향/최대 악화폭, 허용 범위 |
| **서버** | 9 | 비교 대상, 데이터셋 스냅샷, 랜덤 시드 목록, Split 시드, Test 비율, Validation 비율, 학습 설정 참조, 대상 데이터 · 기간, 스냅샷 재사용 |

> 원안은 위 9개 중 8개를 LLM이 창작하게 했다. #536에서 전부 호출자 소유로 옮겼다 —
> §개정 참조.

선택 섹션 `보조 관측 지표`와 `선행 연구 참조`는 호출자가 채우거나 heading을 생략한다.
`결과 (에이전트가 채웁니다)`는 실험 종료 후이므로 #494 소유다.

**`허용 범위`를 사용자가 갖는 이유는 안전이다.** 이 체크박스는 "prod 모델 계약
(`src/features/model_contract.py`) 수정 허용", "Feast 정의 수정 허용", "champion 승격
검토"를 정한다. 실행기가 수정할 수 있는 파일 범위의 정본이므로, **에이전트가 자기
권한을 스스로 넓히게 두지 않는다.**

**서버가 실행 설정 9개를 갖는 이유는 정확성이다.** 이 묶음에는 정확 문자열 옵션
5개(`비교 대상` 3, `스냅샷 재사용` 2)와 정책값이 모두 들어 있다. 창작 대상이 아니라
참조 대상이다.

**`대상 데이터 · 기간`은 고정 문자열이 아니라 발행 시점에 계산한다.** `issue_authoring.
training_window()`가 요청이 들어온 시각을 KST 날짜로 변환해 `[어제 - 29일, 어제]`
30일 구간을 매번 새로 만든다(`docs/specs/2026-07-24-action-log-slice-semantics.md`의
`dt BETWEEN P-30 AND P-1` 소비 계약을 따른다). 서버가 시계를 직접 읽는 대신 날짜를
인자로 받아 테스트가 실행 날짜에 흔들리지 않게 한다.

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

## 결정 4 — 호출자는 heading을 타이핑하지 않는다

호출자는 자기 담당 필드를 **값으로만** 보내고, 서버가 heading과 결합해 최종 본문을
조립한다.

```
요청       {"fields": {"hypothesis": "...", "primary_metric_name": "roc_auc", ...}}
              ↓
서버 조립   <!-- experiment-id: <uuid> -->
            ### 연구 가설
            ...
```

heading 21개 문자열, U+00B7 가운뎃점, 정확 문자열 옵션, 체크박스 label을 호출자가 만들
일이 없어진다. **fail-closed 거부 사유의 대부분이 구조적으로 사라진다.**

> 원안에서는 이 결정이 "LLM이 heading을 만들다 틀리는 것"을 막기 위한 것이었고, 약
> 2,500자짜리 프롬프트 고정분을 근거로 들었다. #536에서 LLM을 제거해 그 근거는 사라졌지만
> **결정 자체는 그대로 유효하다** — 값과 구조를 분리하는 이유는 호출자가 사람이든
> 에이전트든 같다.

**작성 가이드 파일은 지우거나 줄이지 않는다.** `docs/guides/auto-research-issue-authoring.md`
(#490/PR #501)는 사람과 터미널 에이전트를 위한 문서이며 `gh issue create` 절차와 복구
절차가 그 소비자에게는 여전히 참이다.

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

**`/chat`의 Codex는 이 명령을 실행할 수 없다.** `_codex_environment()`가 넘기는 것은 `CODEX_HOME`,
`HOME`, `TMPDIR`, `XDG_CACHE_HOME`, `XDG_STATE_HOME`, `PATH`뿐이라 `gh`가 PATH에 있어도
인증이 없고, `--sandbox read-only`이며 작업 디렉터리에 저장소가 없다.

## 결정 6 — 발행 전 파서 검증을 하지 않는다

`tools/auto_research_issue_branch.py`를 런타임에서 호출하지 않는다. `tools/`는 API
이미지에 들어 있지 않고(`api.Dockerfile`은 `agent_orchestration/`만 복사한다), 넣으면
서비스 코드가 스크립트 계층에 의존하게 된다.

서비스 검증과 parser 계약이 드리프트해 잘못된 본문이 실제 이슈로 열리면 Phase 1
executor의 branch 생성은 본문을 다시 파싱하지 않는다. 따라서 branch가 이미 생길 수
있지만 후속 promotion parser가 입력을 fail-closed로 거부한다. 복구는 기존 branch를
재사용하지 않고 §결정 7의 멱등 경계를 따라 새 실험으로 재제출한다.

**대신 테스트가 조립 결과를 실제 파서로 검증한다.** 런타임은 `tools/`를 import하지
않지만 CI는 저장소 전체를 본다. 이 방식으로 런타임 의존성을 늘리지 않고 검증만 얻는다.

## 결정 7 — 발행은 별도 endpoint이며 멱등이다

```
POST /experiments/{experiment_id}/issue
```

`POST /experiments`와 분리한다. 후자는 순수 DB 쓰기이고 즉시 응답하는 반면, 발행은
`gh` 서브프로세스라는 외부 부작용을 갖고 실패 지점이 다르다. 호출자는 두 번 호출한다.

요청은 다음 둘을 받는다. 응답은 `issue_number`, `issue_url`, `issue_branch`다.

| 키 | 타입 | 필수 |
| --- | --- | --- |
| `fields` | `IssueSubmission`(§개정) | 필수 |
| `allowed_scope` | `prod_model_contract` / `feast_definition` / `promotion` 중 0개 이상 | 선택, 기본 `[]` |

`fields`가 없으면 발행할 값이 없으므로 422다 — 서버가 대신 만들지 않는다.

`IssueSubmission`의 필드는 `title`, `hypothesis`, `change`, `primary_metric_name`,
`primary_metric_direction`, `minimum_primary_delta`, `guardrail_metric_name`,
`guardrail_metric_direction`, `maximum_guardrail_regression`과 선택
`secondary_metrics`·`related_work`다. 정본은
`agent_orchestration/app/experiments/issue_authoring.py`이며 `extra="forbid"`라
서버 소유 값(시드·split 등)을 요청으로 덮어쓸 수 없다.

### 본문 조립과 발행을 분리한다

한 요청 안에서 처리하되 **본문을 DB에 먼저 커밋한 뒤 발행한다.**

```
① issue_body가 이미 있나?
     없음 → 요청 fields + 서버 소유 값으로 조립 → Experiment.issue_body 저장 (commit)
     있음 → 그대로 사용
② issue_number가 이미 있나?
     있음 → 기존 값 반환
     없음 → 저장된 본문으로 gh issue create → issue_number / issue_branch 기록
```

**본문을 저장하지 않으면 재시도가 다른 본문을 발행한다.** 조립은 순수 함수지만 입력이
전부 결정론적이지는 않다 — `대상 데이터 · 기간`을 `training_window()`가 **발행 시점의
KST 날짜**로 계산하므로, `gh` 실패 후 자정을 넘겨 재호출하면 학습 구간이 하루 밀린 본문이
나간다. 저장해 두면 재시도가 **같은 본문으로** 발행되어 결정론적이고, 발행에 실패했을 때
"무엇을 발행하려 했는지"가 DB에 남는다.

**①에서 저장된 본문이 우선이다.** 발행 실패 후 다른 값으로 재호출해도 저장된 본문이
나간다 — 본문이 커밋된 시점에 실험 정의가 봉인된 것으로 본다. 이것이 뚫리면
`criteria_id`가 호출자의 재시도만으로 조용히 바뀐다. 정의를 바꾸려면 새 실험을 만든다.

**고착된 본문의 복구.** `regenerate` 플래그는 #536에서 삭제했다. 저장된 본문이 파서를
통과하지 못해 브랜치가 생기지 않는 상황은 이제 다음 순서로 좁혀진다.

1. 대부분 발생하지 않는다 — 호출자 필드는 `IssueSubmission`이 파서와 같은 규칙으로
   검사해 **이슈가 열리기 전에** 422로 끊는다.
2. 남는 위험은 서버 소유 값이다(`ExperimentDefaults`, `training_window()`). 이쪽이
   파서를 깨면 설정 오류이므로 실험 하나가 아니라 **모든 발행**이 깨진다. 복구는
   설정을 고치는 것이고, 개별 실험의 본문 재생성으로 해결할 문제가 아니다.
3. 그럼에도 개별 실험이 고착되면 **새 실험을 만들어 다시 제출한다.** 고착된 실험은
   `CREATED`로 남으며 이슈가 없으므로 폐루프에 들어가지 않는다.

### 멱등성 3중 방어

| 순서 | 검사 | 결과 |
| --- | --- | --- |
| 1 | `Experiment.issue_number`가 이미 있나 | 발행하지 않고 기존 값 반환 (201) |
| 2 | 일일 발행 상한 초과했나 | 429 |
| 3 | `gh` 성공 후 DB 쓰기 실패 | 본문 marker로 GitHub에서 기존 이슈 조회 |

**1번의 재호출도 201을 반환한다.** 새로 발행했는지 기존 값을 반환했는지로 상태 코드를
가르지 않는다 — 호출자 입장에서는 "이 endpoint를 부르면 이슈 좌표를 받는다"만 참이면
되고, 항상 201로 두는 편이 분기 없이 단순하다.

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

> #536 이후 발행 endpoint는 가설을 요청의 `fields.hypothesis`(≤2000자)로 받는다.
> `ExperimentCreate.hypothesis`의 8192자 상한은 실험 행을 만드는 쪽에 그대로 남는다.

## 구성 요소

```
agent_orchestration/app/experiments/
├── issue_authoring.py 신규 · 순수 함수. 요청 필드 검증 + 서버 값 → 본문 문자열
├── github_issues.py   신규 · gh CLI 경계. 서브프로세스·환경 화이트리스트·timeout·오류 분류
├── service.py         기존 · 생성→저장→발행 2단계 오케스트레이션, 멱등성·상한 검사
├── router.py          기존 · endpoint 추가
├── models.py          기존 · issue_body / issue_number / issue_branch 컬럼
└── schemas.py         기존 · 요청·응답 계약
migrations/versions/0003_experiment_issue_lineage.py   신규
```

`issue_authoring.py`를 순수 함수로 유지하는 것이 중요하다 — **본문 조립을 GitHub 없이
단독으로 테스트**할 수 있어야 §테스트의 파서 대조가 성립한다.

`github_issues.py`는 `llm.py`가 `/chat`의 LLM 바깥 경계를 맡는 것과 같은 위치다. 테스트에서
GitHub 호출만 갈아 끼울 수 있고, 나중에 별도 worker로 옮길 때도 이 모듈만 이동한다.

### 스키마

`Experiment`에 다섯 컬럼을 1급으로 추가한다.

| 컬럼 | 타입 | 채워지는 시점 |
| --- | --- | --- |
| `issue_body` | `Text` | 본문 생성 직후 (발행 **전**) |
| `issue_title` | `String(256)` | 본문 생성 직후, `issue_body`와 같은 commit (발행 **전**) |
| `issue_number` | `Integer` | 발행 성공 후 |
| `issue_branch` | `String` | 발행 성공 후 |
| `issue_published_at` | `DateTime` | 발행 성공 후 |

- **`issue_body`가 발행보다 먼저 커밋되는 것이 핵심이다.** §결정 7의 재시도 결정성이
  여기에 의존한다. 길이 제약을 두지 않는다 — `Text`이고, 조립된 본문은 fixture 기준
  약 1,300자다.
- **`issue_title`을 `issue_body`와 별도로 저장한다.** 저장하지 않으면 재발행 시 본문에서
  제목을 복원해야 하는데, 그 복원 규칙(예: `### 연구 가설` 다음 줄 요약)이 호출자가 준
  실제 `title`과 달라 재발행마다 제목·브랜치 이름이 흔들린다. 상한을 GitHub 이슈 제목
  상한(256자)과 같게 둔다 — `LlmIssueFields.title`은 120자, `hypothesis`(fallback)는
  8192자까지 허용해 그보다 넉넉한 값이 저장으로 흘러들 수 있기 때문이다.
- **`issue_published_at`은 `updated_at`을 대신하지 못한다.** `updated_at`은
  `onupdate=func.now()`라 상태 전이·metric 기록 등 발행과 무관한 UPDATE에도 갱신되므로,
  며칠 전 발행된 실험이 오늘 수정되면 "오늘 발행"으로 잘못 집계되어 일일 상한 질의가
  왜곡된다.
- `ExperimentMetadata`로 대신할 수 없다. `key` `String(64)` / `value` `Text`라 형식이
  보장되지 않고, 더 큰 문제로 **metadata는 `create_experiment()`가 생성 시 1회만
  기록하며 갱신 endpoint가 없다.** 다섯 값 모두 생성 이후에 확정된다.
- 머지 시점의 head가 `0002_experiment_steps`(#518 실험 Step 추적)이므로
  `down_revision = "0002_experiment_steps"`로 연결하고 `upgrade()`/`downgrade()`를
  대칭으로 작성한다. 다섯 컬럼을 **한 revision**에 넣는다.
- 기존 행이 있으므로 **전부 nullable**로 추가한다.
- `issue_number`에 index를 두되 **unique 제약은 두지 않는다** — 이슈 1건이 실험 N건을
  가질 수 있다(대시보드 #338이 주 소비처다).
- `ExperimentResponse`에는 `issue_number` / `issue_branch`만 노출한다. `issue_body`는
  이슈 본문으로 이미 공개되어 있고 목록 응답을 크게 만들 뿐이다. `issue_title`은 재발행
  전용 내부 값이고, `issue_published_at`은 일일 상한 질의 전용이라 둘 다 노출하지 않는다.
- `models.py` 모듈 docstring이 migration과의 동일성을 단언하므로 같은 커밋에서 갱신한다.

## 실패 처리

| 실패 | 처리 | 재호출 시 |
| --- | --- | --- |
| 호출자 필드가 형식 위반 | **422. 이슈도 `issue_body`도 만들어지지 않는다** | 값을 고쳐 다시 호출 |
| `issue_body` 저장 실패 | 실패 | 같은 값으로 다시 조립 |
| `gh` 실패 | 사유를 분류해 알리되 **요청 안에서 재시도하지 않는다** | **저장된 같은 본문으로 발행** |
| `gh` timeout | 프로세스 그룹 회수 후 실패 (`codex.py` 패턴) | **저장된 같은 본문으로 발행** |
| 발행 성공 후 DB 쓰기 실패 | 실패 | marker 조회로 기존 이슈를 찾아 복구 |
| 저장된 본문이 후속 파서를 통과하지 못함 | promotion 입력을 fail-closed로 거부. 이미 생성된 Phase 1 branch는 자동 삭제하지 않음 | §결정 7의 복구 순서 (새 실험으로 재제출) |

`issue_body` 커밋을 경계로 **앞쪽 실패는 재조립, 뒤쪽 실패는 재발행**으로 갈린다.

상태 기계는 건드리지 않는다. 발행 여부는 `issue_number`의 `NULL` 여부로 표현하며,
`CREATED → RUNNING` 전이는 실험 실행기(#492)의 몫이다.

## 설정

| 변수 | 용도 |
| --- | --- |
| `ORCH_GITHUB_TOKEN` | `gh`에 `GH_TOKEN`으로 전달. `issues: write` 전용 |
| `ORCH_GITHUB_REPOSITORY` | `owner/repo`. 발행 대상 고정과 URL 검증 |
| `ORCH_BASELINE_GITHUB_APP_ID` | 이슈 발행 전에 `heads/dev`를 읽는 Contents read App ID |
| `ORCH_BASELINE_GITHUB_APP_INSTALLATION_ID` | baseline reader App installation ID |
| `ORCH_BASELINE_GITHUB_APP_PRIVATE_KEY_PATH` | API Pod에 read-only mount한 baseline reader private key 경로 |
| `ORCH_GH_TIMEOUT_SEC` | `gh` 서브프로세스 상한 |
| `ORCH_ISSUE_DAILY_LIMIT` | 일일 발행 상한 |
| `ORCH_EXPERIMENT_DATASET_SOURCE` | `데이터셋 스냅샷` heading에 쓰는 서버 소유 데이터 출처 좌표 |
| `ORCH_EXPERIMENT_TRAINING_CONFIG_REF` | `학습 설정 참조` heading에 쓰는 서버 소유 학습 설정 좌표 |

`load_settings()`가 기존 토큰들과 같은 방식으로 읽고, `.env.example`에 기재한다.
필수 환경 변수를 도입하므로 같은 PR에서 `README.md`와
`.claude/docs/agent-project-reference.md`를 갱신한다.

## 테스트

- **서버가 조립한 본문이 실제 `parse_issue_input`을 통과한다.** 호출자 담당 필드를
  guardrail 유무·지표 방향 등으로 바꿔가며 검증한다. 결정 6의 위험을 CI가 대신 막는다.
- **`IssueSubmission`과 파서 상수가 어긋나면 실패한다.** `_COMPARISONS`,
  `_SNAPSHOT_REUSE`, `_SCOPE_LABELS`, `_METRIC_DIRECTIONS`, `_HEADING_NAMES` 대조.
- **서버가 쓰는 시드가 `POLICY_SEEDS`와 같다.**
- **본문 marker가 파서와 봉인 식별자에 영향을 주지 않는다.**
- **`선행 연구 참조`의 유무가 `criteria_id`·`reproducibility_id`를 바꾸지 않는다.**
- **형식 위반이 이슈 발행 전에 422로 끊긴다.** 지표 방향·소수 형식·guardrail 동반
  선언·`### ` 삽입·ASCII 없는 제목을 각각 확인한다.
- **`gh` 실패 후 재호출이 저장된 본문으로 발행한다.** 두 번의 발행 본문·제목이 같은지와,
  다른 값으로 재제출해도 저장된 본문이 나가는지로 고정한다.
- `gh` 경계를 스텁으로 대체해 멱등성 3중 방어·상한·실패 분류를 검증한다.
- **Streamlit 폼은 `streamlit.testing.v1.AppTest`로 스크립트를 실제 실행해 검증한다**
  (`tests/test_ui_submission_app.py`). 순수 함수 테스트로는 위젯 렌더링 버그를 잡지
  못한다 — #536에서 `st.form` 안 `disabled` 게이팅 때문에 사용자가 guardrail을 입력할
  수 없는 채로 발행되는 버그가 실제로 났다. 이를 위해 `orchestration-ui` 그룹을 `dev`에
  포함해 CI가 streamlit을 설치한다.
- Alembic `upgrade()`/`downgrade()` 대칭.

## 완료 조건

- 로컬에서 사전등록 필드를 보내면 실제 `[AR]` 이슈가 열리고, API가 이슈 발행 전에
  `dev` tip을 `Experiment.base_dev_sha`로 봉인하는 것을 **1회 실증**한다. infra가
  launcher/executor digest를 배포한 뒤에는 executor Pod가 저장된 SHA에서
  `exp/<번호>-<slug>` 브랜치를 만드는 것을 별도로 실증한다.
- 같은 실험에 발행을 두 번 요청해도 이슈가 하나만 생긴다.
- 발행에 실패한 뒤 재호출하면 저장된 본문 그대로 발행한다.
- 발행된 본문의 `랜덤 시드 목록`이 `42..71`이다.
- `uv run python -m pytest`와
  `uv run --no-sync ruff check agent_orchestration autoresearch tests tools` 통과.

검수 발행물은 **close하되 exp 브랜치는 남긴다.** Phase 1 launcher는 Job 생성 확인 뒤
삭제된 branch를 자동 복구하지 않는다. executor는 기존 GitHub Actions bot marker를 새로
쓰지 않으므로 marker 없는 Phase 1 branch는 promotion workflow 입력이 아니며, marker
작성·신뢰 계약 재설계가 실제 실험 실행 전 후속 gate다. 이 marker는 이 문서 앞부분의
이슈 본문 멱등성 marker(`<!-- experiment-id:... -->`)와 별개다.

## 이 저장소 밖 의존성

| 항목 | 소유 | 없으면 |
| --- | --- | --- |
| `agent_orchestration` Deployment·Service·Ingress | `Autoresearch-infra` | API 이미지는 release가 GAR에 푸시하지만 클러스터에서 실행되지 않아 브라우저로 도달할 수 없다 |
| launcher CronJob·executor Job identity/RBAC/Secret mount | `Autoresearch-infra` | 저장된 `base_dev_sha`가 있어도 executor Pod가 시작되지 않아 exp branch가 생성되지 않는다 |
| `issues: write` 자격 발급·보관 | `Autoresearch-infra` | 배포 환경에서 발행 불가 (로컬은 개인 `gh` 인증으로 가능) |
| Alembic 마이그레이션 실행 | `Autoresearch-infra` | 새 컬럼이 반영되지 않는다. `api.Dockerfile` 주석이 이 책임 경계를 명시한다 |
| Streamlit UI | #484 | 사람이 화면에서 입력할 수 없다 (curl·스크립트는 가능) |

## 미결 항목

- **이슈 작성자 정체성.** PAT를 쓰면 발행된 이슈의 작성자가 개인 계정이 되고, GitHub
  App을 쓰면 봇이 된다. 자격 발급이 `Autoresearch-infra` 소유이므로 그쪽과 함께 정한다.
- **#484의 실제 상태.** 2026-08-04에 COMPLETED로 닫혔으나 저장소에 Streamlit 코드가
  없고 관련 브랜치·PR도 없다. 마지막 코멘트는 "구현을 재개합니다"로 끝난다. 담당자
  확인이 필요하다.
