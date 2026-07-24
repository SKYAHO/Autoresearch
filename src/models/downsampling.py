"""Negative downsampling과 그 확률 보정(calibration)의 순수 함수.

전체 CTR 학습 파이프라인 기준 이 모듈이 담당하는 구간:
- **담당**: (a) train split에서 negative를 비율만큼 남기는 downsampling과
  (b) 다운샘플된 분포로 학습된 모델의 출력 확률을 원분포로 되돌리는 He 2014
  보정 — 두 순수 변환 함수만 제공한다. 입력/출력은 배열·프레임·스칼라이며
  외부 상태나 IO가 없다.
- **담당 아님(인접 책임)**: split 자체(`train.py`), 이 함수들을 파이프라인에
  배선하는 것(`train.py`/`evaluate.py`), `scale_pos_weight` 대체 강제와 champion
  승격 게이트(각 배선/승격 경로), `sampling_rate` 기록(MLflow)·서빙 ONNX
  편입(#302)은 이 모듈이 다루지 않는다.

설계·계약: `docs/specs/2026-07-24-negative-downsampling-calibration.md`.
LightGBM 등 무거운 의존성을 import하지 않는다 — 서빙·evaluate 경로가 보정
함수만 가볍게 가져다 쓸 수 있어야 하기 때문이다(numpy/pandas만 사용).
"""

from __future__ import annotations

from typing import Tuple, Union

import numpy as np
import pandas as pd

ArrayLike = Union[np.ndarray, pd.Series, float]


def apply_downsampling_calibration(
    q: ArrayLike, sampling_rate: float
) -> ArrayLike:
    """다운샘플 분포 확률 q를 원분포 확률 p로 되돌린다(He et al. 2014).

    p = q / (q + (1 - q) / w),  w = sampling_rate = negative를 남긴 비율(0<w<=1).

    보정은 q에 대해 단조증가라 순위(ROC-AUC/PR-AUC)는 바뀌지 않고, 확률의
    크기(LogLoss/calibration)만 원분포로 이동한다. 검증은 후자로 한다
    (스펙 결정 5).

    Args:
        q: 모델 출력 확률(스칼라/np.ndarray/pd.Series). [0, 1] 가정.
        sampling_rate: negative를 남긴 비율 w. 0<w<=1. **없으면(1.0) 항등**이라
            downsampling 미사용·하위호환(sampling_rate param이 없는 기존 모델)
            경로가 자동으로 no-op이 된다(스펙 결정 7).

    Returns:
        원분포로 보정된 확률 p. 입력이 pd.Series면 index를 보존한 Series로 반환.

    Raises:
        ValueError: sampling_rate가 (0, 1] 범위를 벗어나면.
    """
    if not (0.0 < sampling_rate <= 1.0):
        raise ValueError(
            f"sampling_rate는 (0, 1] 범위여야 합니다(negative를 남긴 비율): {sampling_rate}"
        )
    if sampling_rate == 1.0:
        return q  # 항등 — 보정할 것이 없음(downsampling 미사용/하위호환)

    if isinstance(q, pd.Series):
        values = q.to_numpy(dtype=float)
        p = values / (values + (1.0 - values) / sampling_rate)
        return pd.Series(p, index=q.index, name=q.name)
    if isinstance(q, np.ndarray):
        return q / (q + (1.0 - q) / sampling_rate)
    # 스칼라
    return q / (q + (1.0 - q) / sampling_rate)


def downsample_negatives(
    X: pd.DataFrame,
    y: pd.Series,
    sampling_rate: float,
    random_state: int = 42,
) -> Tuple[pd.DataFrame, pd.Series, float]:
    """train split의 negative만 sampling_rate 비율로 남긴다(positive는 전량 유지).

    **train split 전용**이다 — val/test는 원분포를 유지해야 하므로 이 함수를
    거치지 않는다(스펙 결정 3). 호출자(`train.py`)가 split 이후 train 부분에만
    적용한다.

    Args:
        X: train 피처 프레임.
        y: train 레이블(0/1) Series. X와 index 정렬 가정.
        sampling_rate: negative를 남길 비율 w. 0<w<=1. **1.0이면 원본 그대로**
            반환(downsampling off)하고 realized_rate=1.0.
        random_state: negative 샘플링 재현용 시드.

    Returns:
        (X_ds, y_ds, realized_rate). realized_rate는 **실제로 남은 비율**
        (kept_neg / orig_neg) — 확률적 샘플링이라 nominal과 미세하게 다를 수
        있어 이 실현값을 기록/보정에 쓴다(스펙 결정 7). orig_neg가 0이면
        realized_rate=1.0.

    Raises:
        ValueError: sampling_rate가 (0, 1] 범위를 벗어나면.
    """
    if not (0.0 < sampling_rate <= 1.0):
        raise ValueError(
            f"sampling_rate는 (0, 1] 범위여야 합니다(negative를 남긴 비율): {sampling_rate}"
        )
    if sampling_rate == 1.0:
        return X, y, 1.0

    neg_mask = (y == 0).to_numpy()
    pos_idx = y.index[~neg_mask]
    neg_idx = y.index[neg_mask]
    orig_neg = len(neg_idx)
    if orig_neg == 0:
        return X, y, 1.0

    # negative가 매우 적은 split에서 orig_neg*sampling_rate가 0으로 반올림되면
    # keep_neg=0 → realized_rate=0.0이 되고, 이 0.0이 calibration에 전달되면
    # 0<w≤1 검증에 걸려 파이프라인이 중단된다. negative를 전부 없애는 건 학습에
    # 무의미하므로 최소 1건은 남긴다(realized_rate는 그만큼 nominal보다 커진다).
    keep_neg = max(1, int(round(orig_neg * sampling_rate)))
    rng = np.random.default_rng(random_state)
    kept_neg_idx = pd.Index(rng.choice(neg_idx.to_numpy(), size=keep_neg, replace=False))
    realized_rate = keep_neg / orig_neg

    # positive 전량 + 남긴 negative를 합치고, 원래 순서(index)를 유지한다.
    kept_idx = pos_idx.union(kept_neg_idx)
    kept_idx = y.index[y.index.isin(kept_idx)]  # 원본 순서 보존
    return X.loc[kept_idx], y.loc[kept_idx], realized_rate
