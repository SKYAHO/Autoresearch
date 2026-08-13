# 리랭킹 지표 정합 — 평가 전용 패스스루 컬럼과 유저 단위 grouped AUC (2026-08-04)

> 상태: 설계 확정, 구현 대기. 구현 이슈 #505.
> 이 문서는 **왜 지금 지표를 바꾸는지**와 **동작 계약**의 정본이다.
> 주 지표 교체와 판정 엔진 일반화는 #493이, 실험 실행기는 #492가 소유한다.

## 목적

제품 목표는 **유튜브 리랭킹**인데 실험 판정 지표는 **전역 분류 성능**이다. 둘이
일치하는지 한 번도 확인한 적이 없다. 이 문서는 그 차이를 **측정 가능하게** 만드는
최소 변경을 정의한다.

## 문제 — 실측

| 계층 | 슬레이트(유저별 후보 목록) 구조 | 근거 |
| --- | --- | --- |
| action log (원천) | **있음** | `autoresearch/action_log_generation/schema.py:48,51,110` — `event_type`, `rank`, `exposure_rank` |
| `training_entity` (spine) | 부분적으로 있음 | `autoresearch/model_training/build_training_dataset.py:199-200` — `(user_id, video_id, event_timestamp, clicked)` |
| **최종 학습 CSV** | **없음** | `autoresearch/model_training/build_training_dataset.py:690` |

핵심은 마지막 줄이다.

```python
features[[*MODEL_FEATURE_COLUMNS, *experiment_columns, "clicked"]].to_csv(...)
```

CSV를 쓰는 순간 `user_id`·`video_id`·`event_timestamp`가 **전부 잘려 나간다.** 남는
것은 21피처 + `clicked` = 22개 물리 컬럼뿐이다. 그 위에서 `autoresearch/model_evaluation/evaluate.py:50`이
전역 ROC-AUC를 계산한다.

```python
roc_auc_score(dataset["clicked"], model.predict_proba(features)[:, 1])
```

순위 지표(NDCG / MRR / Recall@k / Precision@k)는 저장소 전체에 **0건**이다.

### 왜 전역 AUC가 리랭킹 지표로 불충분한가

1. **그룹 단위가 다르다.** ROC-AUC는 "무작위 양성이 무작위 음성보다 높은 점수를 받을
   확률"이므로 그 자체로 순위 지표가 맞다. 문제는 **전체 (유저, 영상) 쌍에 대해
   전역으로** 계산한다는 점이다. 리랭킹 품질은 **한 유저의 후보 목록 안에서의 순서**다.
   전역 AUC가 높아도 유저별 목록 안 순서는 나쁠 수 있고 그 반대도 가능하다.
2. **위치 가중이 없다.** 리랭킹은 실제로 노출되는 상위 k개만 의미가 있는데, ROC-AUC는
   500등에서의 개선과 2등에서의 개선을 동일하게 센다.

이 문서는 **1번만** 다룬다. 2번은 §후속 판단 분기 참조.

## 왜 지금인가

실험 실행기(#492)가 만들어지기 **전에** 지표를 맞추면 재작업이 0이다. 지표가 틀린
채로 실행기를 돌리면 그 실행기가 생산한 모든 판정이 재작업 대상이 된다. 그리고 이
작업은 실행기를 **전혀 기다리지 않는다** — 기존 스냅샷만으로 완결된다.

## 범위

**포함:**

- 평가 전용 **패스스루 컬럼** 개념 도입
- `user_id`를 패스스루 컬럼으로 학습 CSV와 held-out test set에 보존
- 유저 단위 grouped ROC-AUC 산출, 기존 전역 지표와 **병기**
- 패스스루 컬럼이 모델 입력으로 새지 않음을 강제하는 계약 가드

**제외 (의도적):**

- 주 지표 교체, 판정 엔진의 `PRIMARY_METRIC = "roc_auc"`
  (`autoresearch/model_evaluation/experiment_evaluation.py:45`) 하드코딩 해제 → **#493**
- NDCG@k / Recall@k, `rank` 패스스루 → 이 문서의 실측 결과로 결정 (§후속 판단 분기)
- 실험 실행기 → **#492**
- `MODEL_FEATURE_COLUMNS` 21개의 이름·개수·순서 변경 → 하지 않는다

## 설계 결정

### D1. 모델 입력 목록과 데이터셋 컬럼 목록을 서로 다른 정본으로 분리한다

현재 저장소는 이 둘이 사실상 같아서 그룹화가 막혔다. "모델에 들어가지 않지만 데이터셋에
동승하는 컬럼"이라는 개념이 없다.

### D2. `extra_features` 경로를 재사용하지 않는다

현재 여분 컬럼 통로는 `extra_features` 하나뿐인데, `autoresearch/feature_engineering/model_contract.py:100`
`resolve_experiment_feature_columns()`가 그것을 **prod 계약 뒤에 붙여 모델 입력으로
승격**시킨다(#405의 의도된 동작이다). `user_id`를 이 경로로 넣으면 그대로 모델 피처가
되어 **유저 암기(memorization)**가 발생한다 — capability probe round_003에서 ablation으로
규명된 실패 패턴과 같은 종류다. 따라서 별도 개념이 필요하다.

### D3. 패스스루 컬럼은 CSV **맨 끝**(`clicked` 뒤)에 붙인다

기존 22컬럼 **접두부가 그대로 보존**되므로, 위치 기반으로 읽는 기존 소비자가 깨지지
않는다. `MODEL_FEATURE_COLUMNS` 순서와 ONNX 텐서 해석에는 아무 영향이 없다.

### D4. 주 지표를 교체하지 않고 병기한다

교체하면 exp_001~003, round_001~004의 과거 판정과 **비교 불가**가 된다. 병기하면
"전역과 grouped가 실제로 갈라지는가"를 데이터로 답할 수 있고, 그 답이 다음 단계를
결정한다.

## 동작 계약

### 패스스루 컬럼

- 정본: 패스스루 컬럼 이름 집합을 `autoresearch/feature_engineering/model_contract.py`에 선언한다.
  1차 범위는 `user_id` 하나다.
- **불변식:** 패스스루 컬럼은 `feature_columns.json`에 **절대 포함되지 않는다.**
- 가드: 패스스루 이름이 `extra_features`로 들어오면 `FeatureContractError`로 거부한다.
  거부 지점은 **계약 계층** `resolve_experiment_feature_columns()`
  (`autoresearch/feature_engineering/model_contract.py`)다. 조립의
  `resolve_extra_feature_columns()`가 이 함수를 호출하고 학습의 `--extra-features`도
  같은 계약을 거치므로, 계약 계층에 두면 **두 경로가 한 번에 막힌다**. (설계 시에는
  `clicked` 거부와 같은 자리인 조립 계층을 고려했으나, 패스스루 누출은 조립 고유의
  문제가 아니라 계약 문제이므로 상위 계층이 옳다.)
- 조립 fail-closed: 조회 결과에 패스스루 컬럼이 없으면 CSV를 쓰기 **전에**
  `FeatureContractError`로 멈춘다. 조용히 빠뜨리면 grouped 지표를 못 재는 데이터셋이
  만들어지고, 그 사실은 평가 단계에 가서야 드러난다(#454의 `require_extra_feature_columns`
  선례와 같은 이유).
- 평가는 **관대**하다: 데이터셋에 패스스루가 없으면 grouped 지표만 건너뛰고 전역
  지표는 그대로 낸다. 비대칭은 의도적이다 — 조립은 새 데이터의 품질을 강제해야 하지만,
  평가까지 막으면 패스스루 이전에 만들어진 스냅샷의 재현 평가 경로가 끊긴다.
- 조립 출력 컬럼 순서:
  `[*MODEL_FEATURE_COLUMNS, *extra_features, "clicked", *passthrough_columns]`

### 학습 경로 보존

`evaluate`는 `train-model`이 분리 저장한 held-out test set으로만 채점한다
(`autoresearch/cli.py` run-pipeline 3/4 단계 주석). 따라서 패스스루 컬럼은 **split을 거쳐
test set 저장까지 살아남아야 한다.**

**구현 결과 `train.py`는 변경이 필요 없었다.** 분할이 행 단위 위치 인덱싱
(`dataset.iloc[test_positions]`)이라 모든 컬럼이 그대로 따라가고,
`test_df.to_csv(test_set_path)`가 전 컬럼을 기록하며, 모델 입력은 이름 기반 선택
(`train_df[feature_columns]`)이라 패스스루가 자동으로 배제된다. 즉 기존 구조가 이미
패스스루를 지탱하고 있었고, 막혀 있던 것은 **조립이 컬럼을 잘라낸다는 사실 하나**였다.

### grouped ROC-AUC 정의

- **대상 유저:** 해당 평가 셋에서 양성 1개 **이상**과 음성 1개 **이상**을 모두 가진
  유저. 한 클래스만 가진 유저는 AUC가 정의되지 않으므로 제외한다.
- **집계:** 대상 유저별 ROC-AUC의 **매크로 평균**(유저 동등 가중). 유저별 후보 수가
  달라도 큰 유저가 지표를 지배하지 않게 한다 — 리랭킹 품질은 유저 경험 단위이므로
  유저 동등 가중이 옳다.
- **함께 보고할 값 (필수):** 전체 유저 수, 대상 유저 수, 제외 유저 수와 비율.
  제외 비율이 높으면 grouped 지표의 신뢰도가 낮다는 신호이므로 지표만 단독으로
  내보내지 않는다.
- 대상 유저가 0명이면 지표를 `None`으로 보고하고 실패시키지 않는다(관측 지표이며
  판정 지표가 아니다).

## 배포 영향 — 진행 중인 짝지은 비교는 baseline 재학습이 필요하다

컬럼이 하나 늘면 `build_snapshot_manifest()`가 기록하는 `dataset_sha256`과
`schema_sha256`이 이 변경 **이전 스냅샷과 달라진다.**

`autoresearch/model_evaluation/training_comparison.py`의 `_validate_fairness()`는 baseline과
challenger의 `snapshot_sha256`·`snapshot_manifest_sha256` **동일성**을 요구한다.
따라서 이 변경이 배포되는 시점에 **baseline만 이전 스냅샷으로 학습된 진행 중인 비교**가
있다면 `verify-comparison`이 불일치로 실패한다.

이것은 버그가 아니라 **의도된 동작**이다 — 서로 다른 스키마의 스냅샷으로 학습된 두 run은
애초에 공정 비교 대상이 아니다. 다만 운영자가 취할 조치가 정해져 있어야 한다:

> **조치:** 진행 중인 비교는 **baseline을 새 스냅샷으로 재학습**한 뒤 다시 짝지어야 한다.
> challenger만 재학습하면 불일치가 남는다.

이 영향은 스냅샷 스키마가 바뀌는 모든 변경에 공통이며, 이번 변경에 고유한 위험은 아니다.

## 운영 — 조립 fail-closed가 발동했을 때

패스스루 컬럼이 조회 결과에 없으면 조립이 `FeatureContractError`로 멈춘다. 일일 폐루프
DAG가 이 예외로 실패하면 그날 분량은 **원인을 고친 뒤 같은 날짜 구간으로 조립을 재실행**하면
된다 — 조립은 결정론적이고 CSV는 원자적으로 교체되므로(임시 파일 + fsync), 부분 산출물이
남지 않는다.

`user_id`가 사라질 수 있는 조건은 셋이다: spine 쿼리
(`load_training_entity_spine`)의 SELECT 목록 변경, feast entity join key 이름 변경,
그리고 entity 컬럼을 반환하지 않는 FeatureService로의 교체. 셋 다 **코드·설정 변경**이지
데이터 품질 문제가 아니므로, 재실행 전에 변경을 되돌리거나 계약을 함께 갱신해야 한다.

**한계:** 이 가드는 컬럼 **존재**만 본다. 값이 전부 null이어도 통과한다. 그 경우는
평가 단계에서 `GroupedRocAuc.null_key_rows`로 드러나며, 조립을 멈추지는 않는다 — null
비율은 데이터 품질 지표이지 계약 위반이 아니고, 조립을 막으면 그날 학습 전체가 사라지기
때문이다.

## 검증

- 패스스루 컬럼이 학습 CSV와 held-out test set에 보존됨을 단언하는 테스트
- `feature_columns.json` ∩ 패스스루 집합 = ∅ 을 단언하는 테스트
- `extra_features`로 패스스루 이름을 주면 `FeatureContractError`가 나는 테스트
- 기존 22컬럼 계약과 `MODEL_FEATURE_COLUMNS` 21개 순서가 불변임을 단언하는 기존
  테스트 유지 (`tests/test_model_feature_contract.py`)
- grouped AUC 계산의 경계 조건 테스트: 단일 클래스 유저 제외, 대상 유저 0명
- 회귀: 전체 pytest, `ruff check`

## 실측 산출물 (이 작업의 진짜 목적)

동일 스냅샷에서 **전역 ROC-AUC와 grouped ROC-AUC를 나란히 산출한 값**을 이슈 #505에
기록한다. 이 값이 다음 단계를 결정한다.

### 함께 기록해야 할 해석 조건 (숫자만 적으면 오독한다)

**제외되는 유저는 무작위가 아니다.** `train.py`의 분할은
`train_test_split(source_positions, stratify=dataset["clicked"])`로 **행 단위 랜덤**이라,
한 유저의 후보 행이 train/val/test에 흩어진 뒤 test 조각(기본 20%)만 평가에 들어온다.
따라서 **행이 적은 유저일수록 test 조각에서 단일 클래스가 되어 제외**된다.

결과적으로 `scored_groups`는 **고활동 유저 쪽으로 체계적으로 편향**된다. grouped 값은
"전체 유저의 평균 경험"이 아니라 "test 조각에 양성·음성이 모두 남을 만큼 활동적인 유저의
평균 경험"이다.

그러므로 실측 기록에는 다음을 **반드시 함께** 적는다:

- `total_groups` / `scored_groups` / `skipped_groups` / `null_key_rows`
- 제외 비율이 높으면(예: 절반 이상) grouped 값의 대표성이 낮다는 단서

이 편향은 유저 단위 분할(같은 유저를 한 분할에만 넣는 방식)로 줄일 수 있으나, 분할 방식
변경은 과거 실험과의 비교 가능성을 끊으므로 **이 작업의 범위가 아니다.** 실측 결과를 보고
필요하면 별도 이슈로 다룬다.

## 후속 판단 분기

| 실측 결과 | 해석 | 다음 행동 |
| --- | --- | --- |
| 두 지표가 같이 움직인다 | 전역 AUC가 리랭킹 품질의 충분한 대리 지표 | 현행 주 지표 유지. 그 **근거를 문서로 확보**한 것이 산출물 |
| 두 지표가 갈라진다 | 전역 AUC로 판정하면 리랭킹을 잘못 재고 있었다 | `rank` 패스스루 + NDCG@k / Recall@k 후속 이슈 발행. 원천에 `rank`·`exposure_rank`가 이미 있어 데이터는 존재하나, spine(`training_entity`)이 `rank`를 들고 있지 않아 조립 상류(feature_store_build)까지 범위가 넓어진다 |

어느 쪽이든 **추측이 아니라 실측으로** 결정된다는 것이 이 설계의 요점이다.

## 관련

- #505 — 이 문서의 구현 이슈
- #493 — 판정 엔진 단일화, `PRIMARY_METRIC` 하드코딩 해제 (주 지표 교체의 전제)
- #492 — 실험 실행기
- #494 — 실험 결과 보고 양식
- `docs/specs/2026-08-03-paired-offline-experiment-comparison.md` — 짝지은 비교 계약
- `docs/specs/2026-07-31-experiment-feature-override.md` — #405 실험 피처 오버라이드 경로
