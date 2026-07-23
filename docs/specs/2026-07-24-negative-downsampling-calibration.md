# Negative Downsampling + Calibration 설계 (#300)

> 상태: 설계 확정본(초안). 구현 순서·작업 분해는 짝 문서
> `docs/plans/2026-07-24-negative-downsampling-calibration.md`.

## Context

CTR 학습 파이프라인이 소비하는 실 action log는 클릭이 극히 드물어 클래스
불균형이 심하다. 현재 파이프라인은 불균형을 **`scale_pos_weight`(손실 가중치)
+ stratified split**으로만 다룬다.

- `src/pipeline/config.yaml`: `model.scale_pos_weight: auto` (활성)
- `src/pipeline/train.py:164-172`: `auto`이면 `y_train` 기준 `neg/pos`로 계산
- `src/pipeline/train.py:191-199`: 그 값을 `LGBMModel(scale_pos_weight=...)`로 주입
- `src/models/lgbm_model.py:13,29,57`: LightGBM에 전달

negative downsampling과 이에 대응하는 calibration 단계는 없다. downsampling을
도입하면 모델 출력 확률이 **다운샘플된 분포**를 반영하므로, 원래 분포로 되돌리는
보정이 반드시 함께 가야 한다.

이 문서는 그 도입 방식을 확정한다. 실제 downsampling 비율의 실험/튜닝(어느
값이 지표상 최선인지)은 구현 단계의 실험으로 다루고, 여기서는 **메커니즘과
계약**을 고정한다.

## 결정 요약 (6 + 1)

| # | 항목 | 결정 |
| --- | --- | --- |
| 1 | 보정 규약(w) | `w = sampling_rate` = **negative를 남긴 비율**(0<w≤1). "1/10로 줄임"은 `w=10`이 아니라 **`sampling_rate=0.1`**로 표기 |
| 2 | 보정 공식 | `p = q / (q + (1-q)/w)` (q=모델 출력, p=원분포 보정확률) |
| 3 | downsampling 위치 | `train.py`에서 `train_test_split` **이후, train split에만**. val/test는 원분포 유지 |
| 4 | 보정 적용 위치 | 학습 `evaluate.py`(오프라인 지표) + 서빙 추론, **양쪽** |
| 5 | 검증 지표 | LogLoss / Brier / calibration curve. **AUC 계열 아님**(보정이 monotonic이라 불변) |
| 6 | `scale_pos_weight` 관계 | **대체**(병행 아님). downsampling 도입 시 `scale_pos_weight=1`로 강제 + 회귀 테스트로 가드 |
| 7 | `w` 기록 | **실현값**(nominal 아님)을 MLflow run param + (#302) manifest에 기록 |

## 결정 1·2 — 보정 규약과 공식

He et al. 2014("Practical Lessons from Predicting Clicks on Ads at Facebook")의
downsampling 보정을 쓴다.

```
p = q / (q + (1 - q) / w)
```

- `q`: 다운샘플된 train 분포로 학습된 모델의 출력 확률
- `p`: 원래(비다운샘플) 분포로 보정된 확률
- `w = sampling_rate`: **negative를 남긴 비율**, 0<w≤1 (예: 10%만 남기면 0.1)

**규약을 이렇게 못박는 이유**: 공식과 w의 방향이 어긋나면 서빙에서 확률이
거꾸로 나간다. "감소 배수"(1/10 → 10) 표기를 쓰면 공식이
`p = q/(q + w(1-q))`로 바뀌어야 하는데, 표기 실수 한 번이면 부호가 뒤집힌다.
그래서 **`sampling_rate`(남긴 비율) 한 규약만** 쓰고 "ratio 10" 류 표현은
스펙·코드·주석에서 금지한다.

수치 검산(원본 pos 100 / neg 10,000, true CTR ≈ 0.0099 → negative 10%만 남겨
neg 1,000, 모델이 배우는 q ≈ 0.0909):

```
w = 0.1
p = 0.0909 / (0.0909 + 0.9091 / 0.1) = 0.0909 / 9.1819 ≈ 0.0099   ✓ (원분포 정확 복원)
```

방향(내려감)·크기(원분포 복원) 모두 정합. 공식은 기존 표기 그대로 재사용.

## 결정 3 — downsampling은 train split에만

`src/pipeline/train.py`의 분할은 이미 `train_test_split(..., stratify=clicked)`
로 train/val/test를 나눈다. downsampling은 **이 분할 이후 train 부분에만** 적용한다.

- **val/test는 원분포를 그대로 유지**한다. 다운샘플된 분포에서 지표를 재면
  평가가 거짓말을 한다(He 2014도 train만 샘플링).
- 적용 지점은 `build_training_dataset.py`(조립 단계)가 **아니다**. 그 단계는
  split 이전이라 val/test까지 오염된다. 반드시 `train.py`의 split 이후
  in-memory 단계.
- 부수효과: `training_dataset.csv`는 full로 유지된다(다운샘플은 학습 직전
  메모리에서만). 따라서 #271(OOM) 계열 부담은 늘지 않는다 — 오히려 학습에
  들어가는 행이 줄어 학습 메모리엔 순풍.

## 결정 4 — 보정은 evaluate + 서빙 양쪽

- **학습(`evaluate.py`)**: `src/pipeline/evaluate.py:88`의
  `y_pred_proba = model.predict_proba(X)[:,1]` 직후에 보정을 적용해, LogLoss
  (line 94)와 calibration 지표를 **원분포 기준**으로 측정한다. (AUC/PR-AUC는
  아래 결정 5대로 보정 전/후 동일하므로 영향 없음 — 그래도 일관되게 보정된
  확률로 계산해 코드 경로를 하나로 둔다.)
- **서빙**: 추론 출력에 같은 한 줄을 적용한다. `Div/Sub/Add`만 쓰는 수식이라
  ONNX 표준 연산자로 그래프에 구울 수 있다(#302).

## 결정 5 — 검증 지표는 LogLoss/calibration, AUC 아님

보정 `p = q/(q + (1-q)/w)`는 q에 대해 **단조증가**다. 따라서:

- **ROC-AUC·PR-AUC는 보정 전/후 소수점까지 동일**하다(둘 다 순위 기반).
  "calibration 넣었는데 AUC가 안 변한다"는 **정상**이며 버그가 아니다.
- 실제로 움직이는 건 **LogLoss / Brier / calibration curve**다. 보정이 먹었는지
  확인은 이들로 한다.

**각주(혼란 방지)**: 과거 관측된 "val ≈ 0.52 vs full ≈ 0.90" ROC-AUC 갭은
calibration과 **무관**한 별개 원인(리키지/분포 불일치 계열)이다. 이 작업으로
그 갭이 바뀌지 않으며, "calibration을 넣었는데 왜 그 갭이 그대로냐"는 오해가
없도록 여기 명시해 둔다.

## 결정 6 — scale_pos_weight는 대체(가장 중요한 상호작용)

He 보정 공식은 **"모델이 다운샘플 분포를 그대로 학습했다"**(q가 다운샘플
경험분포의 posterior)를 전제로 한다. downsampling과 `scale_pos_weight`를 **둘 다**
걸면:

1. downsampling이 클래스 비율을 한 번 바꾸고
2. `scale_pos_weight`가 로짓을 `log(scale_pos_weight)`만큼 또 밀어서
3. 모델 출력 q가 다운샘플 분포의 posterior가 **아니게 됨** → 공식 전제 붕괴 →
   이중 보정(과/부족 교정).

따라서 **downsampling 도입 시 `scale_pos_weight`는 대체된다**(병행 아님). 원리로
끝내지 않고 코드로 강제한다:

- `config.yaml`에 downsampling 설정을 추가하고, `sampling_rate < 1.0`이면
  `scale_pos_weight`를 **명시적으로 1로 되돌린다**(또는 파라미터 제거).
- **회귀 테스트로 가드**: `sampling_rate < 1.0`인데 유효
  `scale_pos_weight != 1`이면 학습이 fail-closed 되도록 한다
  (예: `assert not (sampling_rate < 1.0 and scale_pos_weight != 1)`). 나중에
  누군가 둘 다 켠 채 재학습하는 실수를 스펙 단계에서 원천 차단한다.

## 결정 7 — sampling_rate 기록과 서빙 전달

- `sampling_rate`는 **fit이 필요 없는 상수 1개**다(별도 학습 아티팩트 아님).
  확률적 샘플링이면 nominal과 실제가 미세하게 다를 수 있으므로 **실현된
  실제 비율**을 기록한다.
- **기록처**: MLflow run param(`sampling_rate`). 서빙은 champion 모델 버전에서
  이 값을 읽어 보정 한 줄을 적용한다(현재 serving이 registry alias로 모델을
  로드하므로, 같은 모델 버전의 param/tag에서 `sampling_rate`를 함께 읽는 방식).
- **하위호환 기본값**: `sampling_rate` param이 **없는** 모델(#300 이전에 만들어진
  기존 champion — 현재 v6 등)을 서빙이 로드하면, **`sampling_rate = 1.0`으로
  기본 처리**한다(= 보정 항등, no-op). 값이 없다고 죽거나 구현자마다 다른 기본을
  쓰지 않도록 코드에 못박는다. v6처럼 downsampling을 애초에 안 쓴 모델은 이게
  논리적으로도 정확한 동작이다(보정할 게 없음). 결정 1의 `apply_downsampling_
  calibration`이 `sampling_rate == 1.0`에서 항등이므로 경로가 자동으로 일치한다.
- **아티팩트 개수는 3개 유지**(model / feature_columns / categorical_columns).
  calibration은 wrapper 모델이 아니라 상수 + 수식이라 새 아티팩트가 없다.
- **#302와의 경계**: #302가 manifest(`sampling_rate` 포함)와 ONNX 그래프 노드로
  이 값을 정식 편입한다. #300은 값의 산출·기록·오프라인 보정까지 책임지고,
  서빙 추론에의 ONNX 편입은 #302가 이어받는다.

## 순서 가드 (중요)

downsampling이 적용된 모델은 출력 q가 **원분포보다 높게** 나온다. 보정이
end-to-end로 적용되기 전(= 서빙 추론까지)에 이 모델을 champion으로 승격해
서빙에 올리면 **보정 안 된 편향 확률**이 나간다. 따라서:

- downsampling 모델의 champion 승격은 **서빙 보정 적용(#302 또는 #300의 서빙
  한 줄)이 준비된 뒤**에 한다.
- 현재 champion(v6, uncalibrated·downsampling 미적용)은 이 작업과 무관하게
  그대로 유효하다 — #300 완료 시 재학습으로 새 champion이 나오면 그때
  위 순서를 지킨다.

**이 가드는 문서가 아니라 코드로 강제한다.** 이 프로젝트에서 반복된 실패
패턴이 "스펙엔 있는데 코드가 안 지킴"이었다(트렌딩 dedup #298, dataset drift
#301). 사람이 승격 때마다 이 문서를 기억하는 데 의존하면 다음 재학습 사이클에서
누군가(또는 자동 재학습 트리거)가 그냥 champion을 바꿔버린다. 따라서:

- **champion 승격 경로(스크립트/CI)에 fail-closed 게이트**를 둔다:
  승격 대상 모델의 `sampling_rate < 1.0`인데 서빙 보정이 아직 준비되지
  않았으면(#302 미완 = 서빙 추론에 보정 미편입) **승격을 거부**한다
  (`assert not (model.sampling_rate < 1.0 and not serving_calibration_ready)`).
- 가장 안전한 형태: #302가 서빙 보정을 편입하기 전까지 **downsampling 모델
  (`sampling_rate < 1.0`)을 champion 후보에서 스크립트 레벨로 자동 제외**한다.
  `serving_calibration_ready` 판정 기준(예: 서빙 이미지/계약 버전 플래그)은
  #302에서 확정하되, 게이트 자체는 #300에서 넣어 기본 거부로 둔다.

## 완료 조건 (이슈 #300 대응)

- [ ] `config.yaml`로 downsampling 적용 여부·비율(`sampling_rate`)을 제어
- [ ] `sampling_rate < 1.0`이면 `scale_pos_weight`가 1로 강제되고, 아니면
      학습이 fail-closed (회귀 테스트 포함)
- [ ] downsampling은 train split에만 적용, val/test 원분포 유지(테스트로 고정)
- [ ] `evaluate.py`가 보정 확률로 LogLoss/calibration 지표를 산출, 보정 전/후
      비교가 문서화됨(AUC 계열은 불변임을 명시)
- [ ] `sampling_rate` 실현값이 MLflow run param에 기록됨
- [ ] 서빙이 `sampling_rate` 없는 기존 모델(v6 등)을 1.0(보정 없음)으로 안전
      처리함(하위호환, 테스트로 고정)
- [ ] champion 승격 경로에 fail-closed 게이트: `sampling_rate < 1.0`이고 서빙
      보정 미준비면 승격 거부(테스트로 고정) — 문서가 아니라 코드로 강제
- [ ] spec(본 문서)·plan 문서 작성

## 관계·비범위

- **#302**: calibration을 포함한 배포 번들·ONNX/JSON 전환. 본 문서의 결정 4·7이
  #302의 입력이다(수식은 ONNX 노드로, `sampling_rate`는 manifest로).
- **비범위**: ONNX 변환·manifest·서빙 로더 교체는 #302. 본 문서는 downsampling과
  보정 **계약**까지.
- **참고 논문**: He et al., "Practical Lessons from Predicting Clicks on Ads at
  Facebook" (2014).
