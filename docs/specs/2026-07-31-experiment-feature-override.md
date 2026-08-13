# 실험용 피처 오버라이드 경로 계약

- Status: Accepted (구현 완료 — 머지 후 `docs/archive/specs/`로 이동)
- Issue: #405 (#396 ②) · 상위 트래킹: #403
- 근거: `docs/specs/2026-07-29-auto-research-minimum-loop-gaps.md` ②
- 관련: #399(파생 피처 정규 경로, ③ 대체) · #406(실험 네임스페이스) · #407(다중 시드)

## 배경

실험 피처 1개를 넣는 유일한 방법이 **prod 모델 계약 수정**이다.

- `autoresearch/model_training/train.py` — `feature_columns = list(MODEL_FEATURE_COLUMNS)` 하드코딩
- `autoresearch/model_evaluation/evaluate.py` — `require_model_feature_columns()`가 계약과 정확히
  일치하지 않으면 `FeatureContractError`로 중단

`MODEL_FEATURE_COLUMNS`(`autoresearch/feature_engineering/model_contract.py`)는 학습·서빙·Feast가 공유하는
정본이고 **순서까지 고정**돼 있다. ONNX 입력이 이름 없는 순서 배열이라 순서가 틀리면
조용히 오예측한다. 설계 자체는 옳고, 없는 것은 **실험용 예외 통로**다.

#396 실측: `views_per_day` 1개를 계약에 추가하자 테스트 43건이 추가로 깨졌다(학습 10 ·
시뮬레이션 16 · 일일추천 11 · 서빙 4 · 계약 2). 계약 참조 파일은 28개다.

## 범위 경계 — 이 spec이 하지 않는 것 ⚠️

**이 경로는 `training_dataset.csv`에 이미 존재하는 컬럼을 모델 입력으로 승격시킬 뿐,
컬럼을 만들어내지 않는다.**

파생 피처 계산을 여기에 넣지 않는 이유는 셋이다.

1. **이중 소스가 생긴다.** `views_per_day` 같은 값을 `train.py`에서 즉석 계산하면 서빙
   시점에는 Feast online store에 그 피처가 없다. `autoresearch/feature_engineering/feature_builder.py`가
   명시한 원칙 — "세 소비자(학습·시뮬의 Feast ODFV, 서빙 online 후처리, DuckDB 재계산)가
   이 한 벌을 공유해 Training-Serving Skew를 막는다" — 과 정면으로 충돌한다.
2. **책임 경계가 모듈 구조와 어긋난다.** 파생 피처 **계산 본체**는 `feature_builder.py`,
   **정의**는 `feature_repo/feature_definitions.py`, **조립 배관**은 `feast_retrieval.py`가
   소유한다. #405는 그중 어느 것도 아니고 "모델이 어떤 컬럼을 입력으로 받는가"라는
   **계약 계층** 문제다.
3. **fail-closed가 더 안전하다.** "없으면 알아서 계산해 채움"은 실험자가 다른 정의의
   동명 파생 피처를 쓰게 될 여지를 만든다. "없으면 즉시 중단"이 옳다.

컬럼을 만들어내는 정규 경로(Feast ODFV 정의 → `feast apply` → FeatureService
`ctr_training_v1` 갱신)는 **③ = #399**가 소유한다.

## 설계

### 1. 계약 계층 — prod 계약은 불변

`autoresearch/feature_engineering/model_contract.py`에 두 함수를 추가한다. `MODEL_FEATURE_COLUMNS`와
`CATEGORICAL_FEATURE_COLUMNS`는 **한 글자도 바뀌지 않는다.**

```
resolve_experiment_feature_columns(extra) -> tuple[str, ...]
    MODEL_FEATURE_COLUMNS + tuple(extra)
```

**실험 피처는 반드시 prod 계약 뒤에 붙인다.** ONNX 입력 텐서가 이름 없는 순서 배열이라
prod 접두부를 그대로 유지해야 기존 아티팩트·서빙 해석이 깨지지 않는다. 중간 삽입이나
재정렬은 허용하지 않는다.

거부 조건:

- `extra`가 비어 있음 → 실험 경로를 켤 이유가 없음
- `extra` 내부 중복
- `extra`가 `MODEL_FEATURE_COLUMNS`의 이름과 겹침 (이미 prod 피처다)

```
require_experiment_feature_columns(columns, *, extra) -> tuple[str, ...]
    앞 len(MODEL_FEATURE_COLUMNS)개가 prod 계약과 정확히 일치
    나머지가 tuple(extra)와 정확히 일치
```

기존 `require_model_feature_columns()`는 **그대로 둔다.** prod 경로의 엄격한 동등 검사가
느슨해지면 안 된다.

### 2. 학습 — `train.py`

`main(..., extra_features: Sequence[str] | None = None)`

- `None`(기본값)이면 **지금 코드와 동일한 경로.** 기존 호출부·테스트가 영향받지 않는다
- 값이 있으면 `feature_columns = list(resolve_experiment_feature_columns(extra_features))`
- 데이터셋에 없는 컬럼은 **학습 시작 전에** 중단한다(아래 §에러 계약)
- `registry_tags`에 `experiment_features = ",".join(extra)` 를 남긴다

`CATEGORICAL_FEATURE_COLUMNS`는 이번 범위에서 확장하지 않는다. 실험 피처는 수치형만
지원하며, 범주형 실험 피처가 필요해지면 별도 이슈로 다룬다(카테고리 코드 매핑이
서빙 로더와 얽혀 있어 범위가 커진다).

### 3. 평가 — `evaluate.py`

`main(..., extra_features: Sequence[str] | None = None)`

- `None`이면 기존 `require_model_feature_columns()` 그대로
- 값이 있으면 `require_experiment_feature_columns(..., extra=extra_features)`

### 4. 승격 차단 — `promote.py`

`experiment_features` 태그가 붙은 버전을 **후보 선택에서 제외**한다.

거부(REJECTED)가 아니라 제외인 이유가 중요하다. 후보는 버전 번호 최대값 하나로
고르는데, 거부만 하면 실험이 만든 버전이 후보 자리를 차지하고 **그 앞의 정상 prod
후보가 영원히 승격되지 못한다** — 실험을 돌린 날의 champion 갱신이 조용히 유실된다.

등록된 버전이 전부 실험 모델이면 `NO_CANDIDATE` + `EXPERIMENT_MODEL`로 끝낸다.
게이트를 못 넘은 게 아니라 애초에 심사 대상이 없는 상태라, 일일 DAG의 알람 해석이
어긋나지 않는다.

`PromotionReasonCode`에 `EXPERIMENT_MODEL`을 추가한다.

### 5. CLI — `autoresearch/cli.py`

`train-model`과 `run-pipeline`에 `--extra-features` 옵션을 추가한다(쉼표 구분).
미지정이 기본값이므로 공개 batch 계약(batch-contract-v1)은 바뀌지 않는다.

## 에러 계약 — 범위 밖 요청을 #399로 라우팅한다

`--extra-features`에 지정한 컬럼이 학습 데이터셋에 없으면, 원인과 다음 행동이 함께
드러나는 메시지로 중단한다. 팀원이 헷갈릴 때 바로 정규 경로를 찾아가게 하는 것이 목적이다.

메시지에 반드시 포함할 것:

- 없는 컬럼 이름 (여러 개면 전부)
- 그 컬럼이 prod 계약에도 없다는 사실
- 데이터셋에 실제로 있는 컬럼 수 (오타인지 부재인지 구분 가능하게)
- **정규 경로 안내** — Feast ODFV 정의 → `feast apply` → FeatureService 갱신이 필요하며
  #399가 그 경로를 다룬다는 것

예시:

```
실험 피처 ['views_per_day']가 학습 데이터셋에 없습니다.
prod 계약(MODEL_FEATURE_COLUMNS)에도 없는 컬럼입니다.
데이터셋 컬럼 22개: [clicked, age_group, ...]

--extra-features는 데이터셋에 이미 있는 컬럼만 모델 입력으로 승격시킵니다.
컬럼을 새로 만들려면 Feast ODFV 정의 → feast apply → FeatureService(ctr_training_v1)
갱신이 필요합니다. 정규 경로는 #399를 참고하세요.
```

## 완료 조건 매핑 (#405)

| 이슈의 완료 조건 | 이 설계에서 |
| --- | --- |
| 실험 피처를 추가해도 `MODEL_FEATURE_COLUMNS`가 그대로다 | 계약 파일을 수정하지 않는다 |
| 실험 학습·평가가 정상 동작(evaluate가 막지 않음) | `require_experiment_feature_columns` |
| 전체 테스트가 실험 때문에 깨지지 않는다 | `extra_features=None` 기본값 → 기존 경로 무변경 |
| 실험 모델이 실수로 prod 승격되지 않는다 | `experiment_features` 태그 + `promote.py` 게이트 |
| 이슈 템플릿 `허용 범위` 체크박스와 맞물린다 | 아래 § |

### 이슈 템플릿과의 관계

`.github/ISSUE_TEMPLATE/auto_research.yml`의 `허용 범위`에 "prod 모델 계약
(`autoresearch/feature_engineering/model_contract.py`) 수정을 허용한다" 체크박스가 있다.

- 체크가 **꺼져 있으면** → 이 오버라이드 경로를 쓴다(계약 무수정)
- 체크가 **켜져 있으면** → 계약 직접 수정도 가능하지만, 테스트 43건이 깨지는 비용을
  감수한다는 뜻이다

이 대응 관계를 템플릿 설명문에 한 줄 추가한다.

## 에러 타입이 둘인 이유

계약 계층은 `FeatureContractError`, `train.py`의 데이터셋 검증은 `ValueError`를 던진다.
둘 다 "실험 피처 계약 위반"이지만 층이 다르다.

- `FeatureContractError` — **목록 자체**가 계약을 어김(빈 목록·중복·prod 이름 충돌).
  데이터가 무엇이든 성립하는 판단이라 계약 계층이 소유한다
- `ValueError` — **이 데이터셋에서** 쓸 수 없음(컬럼 부재·수치형 아님). 같은 목록이라도
  데이터셋이 바뀌면 결과가 달라진다. `require_binary_labels`가 같은 결로 `ValueError`를
  쓰므로 `train.py`의 데이터 검증 관례를 따른다

호출부가 두 타입을 잡아야 하는 비용은 있지만, 층을 흐리는 것보다 낫다고 판단했다.

## 비범위

- 파생 피처 **생성** (#399 / ③)
- 범주형 실험 피처 (`CATEGORICAL_FEATURE_COLUMNS` 확장)
- 실험 run의 tracking·registry 네임스페이스 분리 (#406)
- 다중 시드 반복 학습 (#407)
- 서빙 경로에서 실험 피처 소비 — 실험 모델은 승격되지 않으므로 서빙에 도달하지 않는다

## 검증 계획

- `model_contract`: 정상 확장 / 빈 목록 / 중복 / prod 이름 충돌 / 순서 검증
- `train`: 기본값이면 기존과 동일한 `feature_columns` / 확장 시 순서가 prod 접두부 + extra /
  데이터셋에 없는 컬럼이면 §에러 계약의 안내를 포함해 중단 / `registry_tags`에 표식
- `evaluate`: 기본값이면 기존 엄격 검증 / 확장 시 통과 / 선언과 다른 아티팩트면 거부
- `promote`: `experiment_features` 태그가 있으면 지표와 무관하게 거부
- 회귀: 전체 스위트가 변경 전과 같은 결과 (`--extra-features` 미사용 경로 무영향)
