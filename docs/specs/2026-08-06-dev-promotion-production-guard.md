# dev 승격의 production 침범 차단 — fail-closed 게이트 (#470 부분 구현)

- **상태**: Proposed
- **날짜**: 2026-08-06
- **이슈**: #470 (작업 범위 8개 중 **2개만** 다룬다 — §2)
- **선행 계약**:
  - `src/tracking/promote.py` — 승격 실행 경로. 이 spec이 확장한다.
  - `src/tracking/promotion_result.py` — `ModelPromotionResult` 계약(`model-promotion-result-v1`).
  - `docs/specs/2026-08-05-hard-retrain-limit-gate.md` (#472) — 같은 "게이트에 조건 추가"
    형태이며, fail-closed 서술 방식을 따른다.

## 목적

**dev 실행이 production champion을 바꾸는 것을 MLflow 변경 전에 차단한다.**

지금 승격 경로에는 "이 실행이 prod냐 dev냐"라는 개념이 **없다.** 이름과 tracking URI가
prod를 가리키면 그냥 실행된다.

## 1. 지금 상태 (착수 전 실측)

### 1.1 모델을 실제로 교체하는 지점은 한 곳이다

```text
src/cli.py promote-model
  → src/tracking/promote.py:main
    → set_model_alias(model_name, champion_alias, candidate_version)   # promote.py:305
```

`set_model_alias` 호출은 저장소 전체에서 **이 한 줄뿐**이다(정의 제외 grep 1건).
따라서 차단 지점을 하나만 만들면 된다.

### 1.2 이미 같은 형태의 방어선이 둘 있다 — 새 패턴이 아니다

| 방어선 | 막는 것 | 위치 |
| --- | --- | --- |
| 실험 네임스페이스 차단 | 실험 모델 이름이면 승격 자체를 거부(`NO_CANDIDATE` / `EXPERIMENT_MODEL`) | `promote.py:109` |
| tracking URI 미설정 차단 | 빈 URI로 조용히 엉뚱한 registry를 보는 것 | `promote.py:124` |
| 서빙 보정 미준비 차단 | downsampling 모델을 보정 없이 champion으로 | `registry.set_model_alias` |

`promote.py:107-108` 주석이 이 spec이 하려는 것을 이미 말로 적어 뒀다:

> 이름을 직접 넘겨 부르는 경우까지 막아 **"실행해도 prod champion에 영향이 없음"**을
> 보장한다.

이 spec은 그 보장을 **환경 축**으로 확장한다.

### 1.3 환경 개념이 승격 경로에 없다

`grep -rn "AUTORESEARCH_ENV" src/tracking/ src/pipeline/` → **0건**.

`promote-model` CLI의 기본값은 `model_name="ctr-model"`, `champion_alias="champion"`으로
**production 좌표**다(`cli.py:824-825`). 즉 인자를 주지 않고 부르면 prod를 대상으로 삼는다.

### 1.4 **지금 위험은 잠재적이다 — 사실대로 적는다**

dev가 이 경로를 부르는 자동 경로는 **아직 없다.**

- `auto-research-dev-promotion.yml`은 이름과 달리 **git 브랜치 머지**를 한다
  (`github.rest.repos.merge`, 후보 커밋을 `dev`에 머지). MLflow alias를 건드리지 않는다.
- production 승격은 인접 저장소 Airflow DAG가 `promote-model`을 호출한다
  (`cli.py` docstring의 `Autoresearch-airflow#137`).

**그래서 이 spec은 "지금 나고 있는 사고"를 막는 것이 아니라, `#470`이 만들려는 dev 자동
승격 경로가 생기기 전에 차단부터 세우는 것이다.** 순서를 뒤집으면(경로를 먼저 만들고
차단을 나중에) 그 사이에 prod를 덮어쓸 수 있는 창이 열린다. 이 저장소가 반복해서 겪은
"스펙엔 있는데 코드가 안 지킴"(`set_model_alias` docstring)을 같은 방식으로 막는다.

## 2. 이 spec이 다루는 범위 — `#470` 8개 중 2개

`#470` 본문의 작업 범위 중 **아래 둘만** 구현한다.

- [x] 모델 lifecycle에서 `AUTORESEARCH_ENV=prod|dev`를 명시적으로 해석하고,
      **미지정 시 기존 production 경로를 유지**한다.
- [x] dev 자동 승격은 dev target에 대해서만 허용하고, **production target으로 향하는
      요청은 MLflow mutation 전에 fail-closed로 차단**한다.

**다루지 않는 것과 사유:**

| `#470` 항목 | 왜 지금 아닌가 |
| --- | --- |
| dev 전용 tracking URI·모델명·alias 좌표 체계 | dev 승격이 **실제로 돌기 시작한 뒤** 필요하다. 지금 만들면 쓰는 곳이 없는 계약이 된다 |
| `PromotionDecision`에 `target_*` 필드 추가 | 위와 같은 이유. 좌표가 없으면 실을 것도 없다 |
| idempotency·rollback/audit 기록 | dev alias를 실제로 옮기기 시작할 때의 문제다 |
| production 좌표 호환성 보존 | 이 spec은 **거부만 추가**하고 기존 경로를 바꾸지 않으므로 자동으로 성립한다(§4.3) |

**나머지 6개 항목은 `#470`에 남긴다.** 이 spec으로 이슈를 닫지 않는다.

## 3. 무엇을 "환경"으로 판단하는가 — **확정: 기존 계약을 재사용한다**

**새로 만들지 않는다.** 저장소에 이미 정본이 있다 —
`feature_repo/env.py::resolve_environment`(`#399`).

```python
ENVIRONMENT_ENV_VAR = "AUTORESEARCH_ENV"   # 값: "prod" | "dev"

def resolve_environment(environment=None) -> str:
    """미설정·공백은 안전하게 prod로 간주한다.
    허용되지 않는 값은 즉시 실패시켜 오타로 인한 조용한 오분류를 막는다."""
```

- **미설정·공백 → `"prod"`** (`None`이 아니다)
- **`"prod"`/`"dev"` 외의 값 → `ValueError`**

**재사용이 가능하다는 근거**: 이 모듈은 stdlib(`os`, `collections.abc`)만 import하므로
feast 의존을 끌고 오지 않는다. 그리고 `autoresearch/jobs/feast_materialize.py:27`이 이미
`from feature_repo.env import ENV_DEV, resolve_environment`로 쓰고 있다 — **패키지 경계를
넘어 이 모듈만 가져다 쓰는 것이 이 저장소의 기존 관례다.**

> **초안 정정**: 이 spec의 첫 초안은 `resolve_environment()`가 `None`을 돌려주는 새
> 계약을 가정하고 "미지정을 prod로 단정하지 않는다"고 적었다. **저장소 전체를 grep하지
> 않고 `src/tracking/`·`src/pipeline/`만 확인한 범위 오류였다.** 기존 계약은 정반대로
> 미설정을 `prod`로 해석하며, 그 사유(환경 셀렉터가 없던 기존 배포가 실수로 dev로
> 떨어지지 않게)도 docstring에 있다. **기존 계약을 따른다** — CLAUDE.md의 "새 추상화보다
> 기존 저장소 패턴을 우선한다".

**결과적으로 의도한 동작은 같다.** 미설정이면 `"prod"`가 되고, §4.1의 차단 조건
(`환경 == dev`)이 거짓이 되어 **차단이 평가되지 않는다.** 현재 모든 승격 실행이
미설정이므로 착지 후 동작이 동일하다(§4.3).

## 4. 차단 규칙

### 4.1 조건

```text
차단한다 ⟺ 환경이 "dev"  AND  대상이 production 좌표
```

**둘 다 참일 때만** 차단한다. 하나라도 아니면 기존 동작 그대로다.

"production 좌표"의 판정은 §7.2에서 확정한다 — 후보는 `model_name`/`champion_alias`가
production 기본값과 같은지, tracking URI가 production을 가리키는지다. **좌표 체계를 새로
만들지 않으므로 "prod가 아닌 것"을 정의하기보다 "prod인 것"을 인식하는 방향으로 좁힌다.**

### 4.2 미설정과 "해석 실패"는 다른 상태다

세 갈래로 갈린다. **가운데 줄이 이 절의 핵심이다.**

| `AUTORESEARCH_ENV` | `resolve_environment()` | 이 게이트의 동작 |
| --- | --- | --- |
| 미설정·공백 | `"prod"` | 차단 조건 거짓 → **기존 경로 유지** |
| `"dev"` | `"dev"` | §4.1 평가 |
| **`"Dev"`·`"development"`·오타** | **`ValueError`** | **승격을 진행하지 않는다** |

**"환경을 읽었는데 해석할 수 없다"는 "환경을 안 읽었다"와 다른 상태다.** 후자만
통과시킨다. 오타를 미설정과 같이 취급해 조용히 넘기면, dev로 띄우려던 실행이
`AUTORESEARCH_ENV=Dev` 하나 때문에 prod로 해석되어 그대로 champion을 바꾼다 — 이 spec이
막으려는 사고가 오타 하나로 재현된다.

**`ValueError`를 그대로 흘려보내지 않는다.** `promote.main`은 실패를 구조화된 결과로
돌려주는 계약이므로(`PromotionExecutionError` + `reason_code`), 다른 실패와 같은 모양으로
바꾼다 — `promote.py:125-129`가 tracking URI 미설정에 대해 이미 하는 방식이다.

```text
ValueError(AUTORESEARCH_ENV 해석 실패)
  → PromotionExecutionError(reason_code=<§4.5의 해석 실패 사유>)
```

이렇게 하지 않으면 raw `ValueError`가 CLI 밖으로 나가 일일 DAG가 사유 없이 죽는다 —
`#495 D-1`("gate step이 죽고 사유가 이슈에 남지 않는다")과 같은 실패 모드다.

### 4.3 하위호환 — 착지해도 아무것도 바뀌지 않는다

`#472`에서 정책 상수를 비워 둔 것과 같은 성질이다. 환경 변수를 아무도 설정하지 않은
현재 상태에서는 §4.2에 따라 차단이 평가되지 않으므로, **기존 prod 승격 경로(Airflow
DAG)는 그대로 동작한다.**

차단이 실제로 켜지는 시점은 **누군가 실행 환경에 `dev`를 명시할 때**다 — 즉 `#470`의
나머지 항목을 구현해 dev 승격을 만들 때다.

### 4.4 MLflow mutation **전에** 막는다

`#470` 본문의 문구를 그대로 지킨다. 차단은 `set_model_alias` **호출 전에** 일어나야
하며, registry를 읽는 것까지는 허용하되 **쓰기는 시도조차 하지 않는다.**

구현 위치는 `promote.py:109`의 실험 모델 차단과 **같은 자리**가 자연스럽다 — 그 분기가
이미 "MLflow 접근 전에 거부"를 하고 있다.

### 4.5 결과 계약 — 새 사유 코드

`ModelPromotionResult`에 새 `PromotionReasonCode`를 더한다.

| `outcome` | `reason_code` | 의미 |
| --- | --- | --- |
| `REJECTED` | **`production_target_from_dev`**(가칭) | dev 실행이 production 좌표를 대상으로 삼았다 |
| `ERROR` | **`environment_unresolvable`**(가칭) | `AUTORESEARCH_ENV` 값을 해석할 수 없다(§4.2) |

두 사유의 `outcome`이 다른 이유: 앞은 **판정 결과**(자격 없음)이고 뒤는 **실행 실패**
(입력이 계약을 벗어남)다. `promote.py`가 `PromotionExecutionError`를 `ERROR`로 사상하는
기존 구조와 맞춘다.

### 4.6 방어선 순서 — **넷 다 고정한다**

이 spec이 둘을 더하면 `promote.main` 앞부분에 차단이 **넷**이 된다. 여러 조건이 한
실행에서 동시에 성립할 수 있으므로(예: dev 실행이 실험 모델 이름으로 prod alias를
노리면서 URI도 비어 있는 경우) 순서를 spec에 고정한다 — 나중에 테스트가 깨졌을 때
"어느 사유가 나와야 맞는가"를 다시 추론하지 않기 위해서다.

| 순서 | 차단 | 결과 | 위치 |
| --- | --- | --- | --- |
| ① | 환경 해석 실패 | `ERROR` / `environment_unresolvable` | **신규** (109행 앞) |
| ② | 실험 네임스페이스 | `NO_CANDIDATE` / `experiment_model` | 기존 `promote.py:109` |
| ③ | dev → production 좌표 | `REJECTED` / `production_target_from_dev` | **신규** |
| ④ | tracking URI 미설정 | `ERROR` / `registry_access_failed` | 기존 `promote.py:125` |

**①이 맨 앞인 이유**: 환경을 해석하지 못하면 ②③④ 어느 판정도 신뢰할 수 없다.
환경변수를 읽는 것뿐이라 I/O도 없다.

**②가 ③보다 먼저인 이유**: 실험 모델은 "애초에 승격 후보가 아니다"라는 더 근본적인
사실이다. 그리고 그 분기가 이미 109행에 있으므로 **기존 동작을 바꾸지 않는 방향**이다.

**③이 ④보다 먼저인 이유**: "이 실행이 이 대상에 올릴 자격이 있는가"는 registry에
접근하기 **전에** 답할 수 있는 질문이다. `#470` 본문의 "MLflow mutation 전에 차단"을
가장 이른 지점에서 지킨다.

> **③의 위치는 §7.3에 종속된다.** "production 좌표"를 `model_name`/`champion_alias`로
> 판정하면 위 순서 그대로다. 그런데 **tracking URI까지 봐야 한다고 결론 나면 ③은 ④
> 뒤로 가야 한다** — URI가 확정되기 전에는 판정할 수 없기 때문이다. §7.3을 정할 때
> 이 표도 함께 갱신한다.

이 순서를 테스트로 고정한다(§9의 4번).

**`NO_CANDIDATE`가 아니라 `REJECTED`인 이유**: 실험 모델 차단(`EXPERIMENT_MODEL`)은
"애초에 승격 가능한 후보가 없다"는 상태라 `NO_CANDIDATE`를 쓴다(`promote.py:110-112`
주석). 이 경우는 다르다 — **후보는 있는데 이 실행이 그것을 그 대상에 올릴 자격이
없다.** 두 상태를 같은 분류로 묶으면 일일 DAG의 알람 해석이 흐려진다.

계약 버전(`model-promotion-result-v1`)은 **올리지 않는다.** 열거형에 값이 추가되는 것은
기존 소비자를 깨지 않는다 — 다만 소비자가 모르는 사유를 만날 수 있으므로 §7.3에서
확인한다.

## 5. 왜 이 범위가 지금 값이 있는가

- **`#546`(실행기 Phase 2 이후)와 독립적이다.** dev 승격 경로가 언제 생기든, 생기기
  **전에** 차단이 있어야 한다.
- **리스크가 낮다.** 새 경로를 만드는 것이 아니라 거부 조건 하나를 추가한다. 현재 모든
  실행이 미지정이라 동작 변화가 없다(§4.3).
- **저장소 패턴 그대로다.** §1.2의 세 방어선과 같은 자리·같은 모양이다. 새 추상화가
  아니다.

## 6. 비목적

- **dev 승격 경로 구현** — 이 spec은 차단만 만든다. 경로는 `#470` 나머지 항목.
- **production 좌표 변경** — 기존 `ctr-model@champion` 계약을 건드리지 않는다.
- **Airflow DAG 수정** — 인접 저장소 소유(`Autoresearch-airflow`).
- **serving 경로** — 이 spec은 registry alias 이동만 다룬다.

## 7. 미해결

### 7.1 환경 판단 방식 — **해소됨(2026-08-06)**

기존 `feature_repo/env.py::resolve_environment`를 재사용한다(§3). 저장소 전체 grep으로
확인했고, 새로 물어볼 것이 없다.

### 7.2 새 사유 코드의 소비자 영향 — **구현 전 필수 확인**

`PromotionReasonCode`를 읽는 곳(일일 DAG 알람, `promotion_result` 소비자, Airflow)이
**모르는 값을 만났을 때 어떻게 되는지** 확인한다. 열거형 파싱이 strict면 새 사유가
소비자를 죽인다.

**이 항목의 우선순위를 낮게 잡으면 안 된다.** fail-closed 게이트를 추가하면서 그
부산물이 알람을 조용히 죽이면 본말전도다 — 게이트가 승격을 막았는데 그 사실이 아무에게도
전달되지 않는 상태가 된다. 구현 전에 확인하고 결과를 이 절에 기록한다.

**신규 사유가 둘이므로 둘 다 확인한다** — `REJECTED`/`production_target_from_dev`와
`ERROR`/`environment_unresolvable`.

**`ERROR` 쪽이 더 중요하다.** `REJECTED`가 안 읽히면 "막았는데 조용하다"이지만,
`ERROR`가 안 읽히면 **"막았는데 왜 막혔는지 아무도 못 읽는다"**가 된다. 후자는 이 spec
전체의 목적과 정면으로 어긋난다 — 오타 하나로 승격이 멈췄는데 운영자가 원인을 모르면,
`AUTORESEARCH_ENV`를 고치는 대신 게이트를 꺼버리는 쪽으로 가게 된다.

확인 대상: `ModelPromotionResult`를 역직렬화하는 모든 경로 — `promotion_result` 소비자,
일일 DAG 알람, `outcome`/`reason_code`로 분기하는 곳 전부. 인접 저장소
(`Autoresearch-airflow`)가 이 계약을 소비한다면 그쪽도 포함한다 — 저장소 밖이면
**요청 내용만 정리해 보고한다.**

### 7.3 "production 좌표"의 판정 기준 — 구현 중 확정

`model_name`/`champion_alias`가 기본값과 같은지로 볼지, tracking URI까지 볼지.
§4.1의 규칙 모양은 이 선택과 무관하게 유지되므로 조사로 답할 수 있다.

### 7.4 승격 게이트 쪽 기존 환경 신호 — 확인 요청 대상

`#450`/`#461`(승격 게이트 파이프라인, hyochangsung)이 **PR 타깃 브랜치(main vs dev)**를
1차 방어선으로 삼는 구조다. 이 spec의 게이트는 **모델 레지스트리 alias 계층**이라 중복은
아니지만, 그쪽에 이미 "지금 실행이 dev인지 prod인지"를 알려주는 신호가 있다면 §4.1이
그것을 소비하는 편이 낫다.

**직접 확인하지 않는다** — 다른 사람 담당 영역이므로 무엇을 물어야 하는지만 정리해
보고한다(세션 규칙).

## 8. 이 작업의 우선순위 — 착수 승인 시 함께 확인

§1.4대로 **지금 이 위험은 잠재적이다.** 8일 스프린트 맥락에서 이 작업이 다른 미해결
항목(인프라 쿼터, `FEAST_ENV=dev` 누락 등)보다 먼저 갈 이유를 이 spec은 제시하지 않는다.
**순서는 이슈 오너 판단이며, plan 승인 시 함께 확인한다.**

이 spec이 제시할 수 있는 근거는 하나뿐이다: **차단은 dev 승격 경로가 생기기 전에 있어야
값이 있다**(§1.4). 그 경로가 언제 생기는지가 우선순위를 정한다.

## 9. 구현 순서 (plan에서 상세화)

1. **§7.2 확인 먼저** — 새 사유 코드를 소비자가 감당하는지. 이게 아니면 게이트가
   알람을 죽인다.
2. `feature_repo.env.resolve_environment` 재사용 + `ValueError` → 구조화된 실패로 변환
   (§3·§4.2)
3. production 좌표 판정 (§4.1, §7.3은 조사로 확정)
4. `promote.py`에 차단 분기 + 새 사유 코드 2종, 우선순위 고정 (§4.4·§4.5·§4.6)
5. 계약 테스트 — **dev 차단 전후로 production `ctr-model@champion`이 변경되지 않음**
   (`#470` 완료 조건 중 이 spec 범위에 해당하는 것)

1~3은 `src/tracking/` 안에서 끝나며 `src/pipeline/`·`autoresearch/`를 건드리지 않는다.
