# Negative Downsampling + Calibration 구현 계획 (#300)

> 짝 문서: `docs/specs/2026-07-24-negative-downsampling-calibration.md`
> (설계 결정·계약). 본 문서는 구현 순서·작업 분해·검증 체크리스트.

## 원칙

- 구조 변경과 동작 변경 분리. downsampling/보정 로직(신규 순수 함수)과 그걸
  파이프라인에 배선하는 커밋을 나눈다.
- 기존 `scale_pos_weight` 경로는 제거하지 않고 **downsampling과 상호배타**로
  묶는다(설정으로 둘 중 하나만 유효).

## 작업 분해

### 1. 보정 함수 (순수, 신규)

- `src/models/` 또는 `src/features/`에 `apply_downsampling_calibration(q,
  sampling_rate)` 순수 함수 추가. 스펙 결정 2 공식 `q/(q + (1-q)/w)` 그대로.
  `sampling_rate == 1.0`이면 항등(보정 없음)으로 반환해 downsampling 미사용
  경로가 자동으로 no-op이 되게 한다.
  - **입력 방어(필수, 스펙 결정 2)**: 진입부에서 `0<w≤1`을 검증해 위반 시
    `ValueError`(`w=0` → 0으로 나누기 방지). `assert`가 아니라 명시적 예외.
- 단위 테스트: 스펙의 수치 검산(q=0.0909, w=0.1 → ≈0.0099), 경계
  (w=1.0 → 항등), monotonicity(입력 순서 보존), **범위 위반(`w=0`/`w>1`/`w≤0`
  → `ValueError`)** 단언.

> **구현 현황**: 위 함수 1·2는 스펙 리뷰 대기 중 `300-impl-...` 브랜치에 선행
> 작성 완료(`src/models/downsampling.py`, 범위 검증·`ValueError` 포함). 테스트
> 18건 통과(수치검산·항등·monotonicity·경계·`ValueError`·positive 불변·
> realized_rate·seed 결정론). 배선(3·4·게이트)은 스펙 승인 후 이어감.

### 2. train split 다운샘플러 (순수, 신규)

- `downsample_negatives(X_train, y_train, sampling_rate, random_state)` — train
  split의 negative만 `sampling_rate` 비율로 남기고 positive는 전량 유지. 실제
  실현된 비율(`realized_rate = kept_neg / orig_neg`)을 함께 반환.
- 단위 테스트: positive 불변, negative가 대략 `sampling_rate`배, `realized_rate`
  반환, `random_state` 결정론.

### 3. train.py 배선

- `config.yaml` `model`에 `sampling_rate`(기본 1.0 = downsampling off) 추가.
- Step 2(split) 이후 train split에만 `downsample_negatives` 적용. val/test 불변.
- Step 5: `sampling_rate < 1.0`이면 `scale_pos_weight`를 1로 강제. **가드**:
  `sampling_rate < 1.0`인데 config가 유효 `scale_pos_weight != 1`(=auto 포함
  실계산값)을 요구하면 명시적 예외로 fail-closed.
  - **끼어드는 위치 주의**: 현재 `train.py:164-172`가 `auto`를 먼저 계산하고
    `191-199`에서 주입한다. 강제 로직은 **auto 계산(164-172) 이후, 주입(191-199)
    이전**에 넣어야 한다 — 순서가 뒤바뀌면 auto가 계산한 큰 값이 강제(=1)를
    덮어써 이중 보정이 그대로 남는다.
  - **다른 밸런싱 옵션 확인**: `is_unbalance` 등 다른 자동 밸런싱 옵션이
    config/`lgbm_model.py`에 없는지 확인(현재 없음). 가드를 `scale_pos_weight`
    단독이 아니라 활성 밸런싱 옵션 일반으로 표현해 이후 추가도 커버.
  - **early stopping 확인**: 현재 `lgbm_model.py:fit()`은 `eval_set`/`callbacks`
    없이 고정 `n_estimators`로 학습 → early stopping 없음(스펙 결정 4의 전방
    가드 참고). 이 스펙 범위에서 early stopping을 새로 켜지 않는다. 만약 켠다면
    val 예측에 보정을 먼저 적용한 뒤 stopping 지표를 재야 한다.
- Step 5 기록: `sampling_rate` **실현값**을 `log_parameters`(run param)에 기록
  하고, `register_model` 직후 `MlflowClient.set_model_version_tag(...,
  "sampling_rate", ...)`로 **모델 버전 tag에도** 기록(서빙 alias 로드 시 직접
  조회용, 스펙 결정 7).

### 3b. champion 승격 게이트 (스펙 순서 가드의 코드 강제)

- champion 승격 경로(`src/tracking/registry.py` 계열 또는 승격 스크립트)에
  fail-closed 게이트 추가: 승격 대상의 `sampling_rate < 1.0`이고 서빙 보정이
  준비되지 않았으면(`serving_calibration_ready`가 아니면) 승격 거부.
- #302 전까지는 **downsampling 모델을 champion 후보에서 자동 제외**가 기본
  동작이 되도록 `serving_calibration_ready` 기본값을 `False`로 둔다(#302가
  True로 켜는 플래그/계약 버전은 #302에서 확정).
- 테스트: `sampling_rate < 1.0` + 미준비 → 승격 거부, `sampling_rate == 1.0`
  (v6류) → 승격 허용.

### 4. evaluate.py 배선

- `src/pipeline/evaluate.py:88` 직후 보정 적용. `sampling_rate`는 MLflow run
  param(또는 model 메타)에서 읽어 전달. **없으면 1.0(항등)** — 하위호환
  기본값(스펙 결정 7). LogLoss(line 94)가 보정 확률로 계산되도록 한다.
  ROC-AUC/PR-AUC는 불변이므로 값 변화 없음(정상).
- calibration curve/Brier 산출을 evaluate 출력에 추가(보정 검증 근거).

### 5. 회귀·통합 테스트

- 가드 테스트: `sampling_rate<1.0 && scale_pos_weight!=1` → 학습 fail-closed.
- val/test 원분포 테스트: downsampling 켠 학습에서 val/test 클래스 비율이
  원본과 동일함을 단언(train만 줄었는지).
- end-to-end: 작은 합성셋으로 train→evaluate, 보정 전/후 LogLoss는 달라지고
  ROC-AUC는 동일함을 단언(스펙 결정 5 고정).
- 하위호환 테스트: `sampling_rate` param이 없는 모델을 evaluate/서빙 경로가
  1.0(항등)으로 처리해 죽지 않고 보정 no-op이 되는지 단언(v6류 케이스).
- 승격 게이트 테스트: `sampling_rate<1.0` + 서빙 보정 미준비 → champion 승격
  거부, `sampling_rate==1.0` → 허용(스펙 순서 가드 고정).

## 검증

```bash
uv run python -m pytest tests/ -q            # 신규 단위/회귀 포함 전체
uv run python -m pytest -v                   # CI 동일
```

- 실 데이터 실험(별도, 코드 확정 후): `sampling_rate` 후보 몇 개로 학습해
  LogLoss/calibration curve 비교, 최종값 결정. 이 실험 결과는 spec에 표로 추가.

## 순서·의존

- 1·2(순수 함수)는 독립. 3·4는 그 위에. 5는 3·4 후.
- **#302 핸드오프**: 서빙 추론에의 보정 적용(ONNX 노드)·manifest 편입은 #302.
  #300은 train/evaluate/기록(run param + **모델 버전 tag**)까지. downsampling
  champion의 서빙 승격은 스펙의 "순서 가드"를 따른다(서빙 보정 준비 후).
- **서빙 캐싱 계약은 #300 스펙(결정 7)이 정의, 구현은 #302**: 서빙이
  `sampling_rate`를 로드 시 1회 읽어(모델 버전 tag에서 직접) 캐싱하고 요청 중엔
  캐시 값만 쓴다 — 이 계약을 #300 스펙에 못박아 #302 구현이 요청당 MLflow
  호출로 새지 않게 한다. #300이 tag를 기록하므로 #302가 읽기만 하면 된다.

## 비범위

- ONNX 변환, manifest, 서빙 로더 교체·서빙 캐싱 구현 → #302
  (#300은 그 입력인 tag 기록 + 캐싱 계약 정의까지).
- `sampling_rate` 최적값 자체의 대규모 튜닝 → 코드 확정 후 실험 트랙.
