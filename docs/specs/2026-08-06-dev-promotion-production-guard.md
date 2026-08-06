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

## 3. 무엇을 "환경"으로 판단하는가 — **미확정**

`#470` 본문은 `AUTORESEARCH_ENV=prod|dev`를 지목했고 Feast에 그 개념이 일부 있다. 다만
승격 경로에는 없으므로 **새로 들이는 것**이다.

이 spec은 **판단 규칙의 모양만 고정**하고 변수 이름·해석 방식은 구현 단계에서 확정한다
(§7.1). 어느 쪽이든 아래 §4의 계약은 바뀌지 않는다.

```text
resolve_environment() -> "prod" | "dev" | None      # None = 미지정
```

**미지정(`None`)은 prod로 간주하지 않는다.** "모르는 것을 prod로 단정"하면 dev 실행이
미지정 상태로 들어왔을 때 그대로 통과한다. 대신 **미지정이면 차단 조건을 평가하지
않는다**(§4.2) — `#472` §4.2의 "관측되지 않은 것을 값으로 바꾸지 않는다"와 같은 결이며,
`#470` 본문의 "미지정 시 기존 production 경로를 유지"와도 맞는다.

## 4. 차단 규칙

### 4.1 조건

```text
차단한다 ⟺ 환경이 "dev"  AND  대상이 production 좌표
```

**둘 다 참일 때만** 차단한다. 하나라도 아니면 기존 동작 그대로다.

"production 좌표"의 판정은 §7.2에서 확정한다 — 후보는 `model_name`/`champion_alias`가
production 기본값과 같은지, tracking URI가 production을 가리키는지다. **좌표 체계를 새로
만들지 않으므로 "prod가 아닌 것"을 정의하기보다 "prod인 것"을 인식하는 방향으로 좁힌다.**

### 4.2 미지정은 평가하지 않는다

```text
환경이 None  →  차단 조건을 평가하지 않는다 (기존 경로 유지)
```

관측되지 않은 것을 "dev다" 또는 "prod다"로 바꾸지 않는다. 지금 모든 실행이 미지정이므로
**이 규칙 덕분에 착지 직후 동작이 완전히 동일하다**(§4.3).

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

## 7. 미해결 — 구현 전 확정

### 7.1 환경 판단 방식

`AUTORESEARCH_ENV` 환경변수를 쓸지, 다른 신호를 쓸지. **이슈 오너 확인이 필요하다** —
`#470` 본문이 그 변수를 지목했으나 승격 경로에 없던 개념이라 새로 들이는 결정이다.
확인 결과를 이 절에 기록한 뒤 구현한다.

### 7.2 "production 좌표"의 판정 기준

`model_name`/`champion_alias`가 기본값과 같은지로 볼지, tracking URI까지 볼지.
§4.1의 규칙 모양은 이 선택과 무관하게 유지된다.

### 7.3 새 사유 코드의 소비자 영향

`PromotionReasonCode`를 읽는 곳(일일 DAG 알람, `promotion_result` 소비자)이 모르는 값을
만났을 때 어떻게 되는지 확인한다. 열거형 파싱이 strict면 하위호환이 깨질 수 있다.

## 8. 구현 순서 (plan에서 상세화)

1. 환경 해석 함수 (§3, §7.1 확정 후)
2. production 좌표 판정 (§4.1, §7.2 확정 후)
3. `promote.py`에 차단 분기 + 새 사유 코드 (§4.4·§4.5)
4. 계약 테스트 — **dev 차단 전후로 production `ctr-model@champion`이 변경되지 않음**
   (`#470` 완료 조건 중 이 spec 범위에 해당하는 것)

1~3은 `src/tracking/` 안에서 끝나며 `src/pipeline/`·`autoresearch/`를 건드리지 않는다.
