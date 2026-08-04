#!/usr/bin/env python3
"""저장된 모델을 held-out test set으로 채점하는 평가 스크립트.

[파이프라인] 학습(train.py)이 분리 저장한 held-out test set과 승격 판정 사이 구간을
담당한다. 모델과 `feature_columns.json`을 로드해 계약을 검증하고, held-out 지표를
산출해 stdout과 호출자(`run-pipeline`)에 제공한다. 학습 runtime이 승격 증거로 쓸
held-out 지표도 여기 정의를 공유해, standalone 평가와 학습이 같은 지표 정의를 쓴다.

[기능] held-out ROC-AUC/PR-AUC/Log Loss/Brier/calibration 요약과 baseline 대비
비교를 산출한다(예측 1회 재사용). 데이터셋에 평가 전용 패스스루 컬럼이 있으면 유저
단위 `grouped_roc_auc`를 **전역 지표와 병기**해 리랭킹 품질을 함께 보고한다(#505).
downsampling 보정은 순위를 바꾸지 않으므로 ROC-AUC/PR-AUC 계열에는 적용하지
않는다(#300).

[비책임] 학습·분할은 `src/pipeline/train.py`, 데이터셋 조립과 패스스루 컬럼 보존은
`src/pipeline/build_training_dataset.py`, 공정 baseline·challenger 비교는
`training_comparison.py`, write-once 증거 계약과 GCS 게시는
`promotion_evidence.py`, 통계적 유의성 판정은 `src/pipeline/seed_sweep.py`와
`src/pipeline/experiment_evaluation.py`가 소유한다. 주 지표를 무엇으로 삼을지와
승격 판정은 이 모듈이 정하지 않는다(#493).
"""

import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Optional

import yaml
import pickle
import numpy as np
import pandas as pd
from sklearn.metrics import (  # noqa: E402
    roc_auc_score,
    average_precision_score,
    log_loss,
    brier_score_loss,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from src.utils.model_utils import load_model, load_feature_columns  # noqa: E402
from src.models.downsampling import apply_downsampling_calibration  # noqa: E402
from src.features.model_contract import (  # noqa: E402
    CATEGORICAL_FEATURE_COLUMNS,
    require_experiment_feature_columns,
    require_model_feature_columns,
)


# grouped 지표의 그룹 키. 반드시 `PASSTHROUGH_COLUMNS`의 원소여야 한다 — 모델 입력이
# 아닌 컬럼만 그룹 키가 될 수 있다(#505). 연결은 계약 테스트가 지킨다.
GROUP_KEY_COLUMN: Final[str] = "user_id"


@dataclass(frozen=True, slots=True)
class GroupedRocAuc:
    """유저 단위 ROC-AUC와 그 값을 얼마나 믿을 수 있는지의 근거(#505).

    ``value``만 떼어 보면 안 된다 — 제외 유저가 많을수록 소수 유저의 값이 전체를
    대표하게 되므로, 집계 대상 수를 함께 읽어야 지표의 신뢰도를 알 수 있다.
    """

    value: float | None
    total_groups: int
    scored_groups: int
    skipped_groups: int
    # 그룹 키가 null이라 어느 그룹에도 귀속되지 못한 행 수. 그룹이 아니므로 위 세 수에
    # 들어가지 않지만, 세지 않으면 조용히 사라져 커버리지 보고가 거짓이 된다.
    null_key_rows: int


def grouped_roc_auc(
    labels: Sequence[int],
    scores: Sequence[float],
    groups: Sequence[object],
) -> GroupedRocAuc:
    """그룹(유저) 단위로 ROC-AUC를 재고 매크로 평균한다(#505).

    전역 ROC-AUC는 전체 (유저, 영상) 쌍에서 계산하므로 **유저를 가로지르는 순서**까지
    점수에 넣는다. 리랭킹 품질은 한 유저의 후보 목록 **안**의 순서이므로, 그룹 안에서
    재고 그룹끼리 평균해야 제품 목표와 같은 것을 잰다.

    매크로 평균(그룹 동등 가중)을 쓴다 — 후보 수가 많은 유저가 지표를 지배하면 "유저
    경험의 평균"이 아니라 "행의 평균"이 된다.

    Args:
        labels: 이진 라벨.
        scores: 모델 점수(순위만 쓰므로 보정 여부는 무관하다).
        groups: 행이 속한 그룹 키(보통 ``user_id``).

    Returns:
        매크로 평균과 집계 대상 수, 그리고 그룹 키가 null이라 귀속되지 못한 행 수.
        양성·음성을 모두 가진 그룹이 없으면 ``value``는 ``None``이다 — 관측 지표이므로
        실패시키지 않는다.
    """
    frame = pd.DataFrame({"label": labels, "score": scores, "group": groups})
    # `groupby`의 기본값 dropna=True는 null 키 행을 그룹으로 세지도, 집계에 넣지도
    # 않는다. 여기서 먼저 세지 않으면 그 행들이 어느 수치에도 남지 않아 커버리지
    # 보고가 실제로 버려진 행을 감춘다. `dropna=False`로 묶지 않는 이유는 서로 무관한
    # 행들이 하나의 가짜 그룹이 되어 의미 없는 AUC가 계산되기 때문이다.
    null_key_rows = int(frame["group"].isna().sum())

    per_group: list[float] = []
    total_groups = 0
    for _, rows in frame.groupby("group", sort=False):
        total_groups += 1
        # 한 클래스만 있는 그룹은 ROC-AUC가 정의되지 않는다(순위를 비교할 상대가 없다).
        if rows["label"].nunique() < 2:
            continue
        per_group.append(float(roc_auc_score(rows["label"], rows["score"])))

    scored_groups = len(per_group)
    return GroupedRocAuc(
        value=(sum(per_group) / scored_groups) if scored_groups else None,
        total_groups=total_groups,
        scored_groups=scored_groups,
        skipped_groups=total_groups - scored_groups,
        null_key_rows=null_key_rows,
    )


# 학습 경로가 승격 증거로 산출하는 held-out 지표 이름. 증거 계약이 인정하는
# allowlist(promotion_evidence.SUPPORTED_HELD_OUT_METRIC_NAMES)와 같은 집합이어야
# 하며, 그 동등성은 tests/test_pipeline_evaluate.py가 고정한다.
# 여기서 promotion_evidence를 import하지 않는 이유는 평가 모듈이 승격 증거
# 계약에 의존하지 않게 하기 위해서다.
HELD_OUT_METRIC_NAMES: tuple[str, ...] = ("roc_auc", "pr_auc", "log_loss")


def _held_out_positive_proba(
    model: object,
    dataset: pd.DataFrame,
    feature_columns: Sequence[str],
) -> np.ndarray:
    """held-out dataset에 학습과 동일한 feature cast를 적용해 양성 확률을 낸다."""
    features = dataset[list(feature_columns)].copy()
    for column in CATEGORICAL_FEATURE_COLUMNS:
        features[column] = features[column].astype("category")
    return model.predict_proba(features)[:, 1]


def evaluate_held_out_roc_auc(
    model: object,
    dataset: pd.DataFrame,
    feature_columns: Sequence[str],
) -> float:
    """held-out test dataset의 원본 예측 ROC-AUC를 계산한다.

    학습 runtime과 standalone 평가가 같은 feature cast·ROC-AUC 정의를 쓰도록
    공통화한다. downsampling 보정은 순위를 바꾸지 않으므로 ROC-AUC에는 적용하지
    않는다.
    """
    proba = _held_out_positive_proba(model, dataset, feature_columns)
    return float(roc_auc_score(dataset["clicked"], proba))


def evaluate_held_out_metrics(
    model: object,
    dataset: pd.DataFrame,
    feature_columns: Sequence[str],
    *,
    sampling_rate: float = 1.0,
) -> dict[str, float]:
    """held-out test dataset의 `HELD_OUT_METRIC_NAMES` 지표를 한 번에 계산한다.

    `predict_proba`는 한 번만 호출하고 지표마다 재사용한다.

    ROC-AUC와 PR-AUC는 순위 기반이라 downsampling 보정에 불변이므로 원본 확률로
    재고, Log Loss는 보정된 확률(원분포 기준)로 잰다 — `main()`의 지표 정의(#300
    결정 5)와 같다. `sampling_rate=1.0`이면 보정은 항등(no-op)이다.

    Args:
        model: `predict_proba`를 제공하는 학습된 모델.
        dataset: `clicked` 라벨과 피처를 가진 held-out test split.
        feature_columns: 모델이 학습에 쓴 피처 순서.
        sampling_rate: 학습에 쓴 negative downsampling 실현 비율.

    Returns:
        지표 이름 → 값 매핑. 키 집합은 `HELD_OUT_METRIC_NAMES`와 같다.
    """
    labels = dataset["clicked"]
    raw_proba = _held_out_positive_proba(model, dataset, feature_columns)
    calibrated_proba = apply_downsampling_calibration(raw_proba, sampling_rate)
    return {
        "roc_auc": float(roc_auc_score(labels, raw_proba)),
        "pr_auc": float(average_precision_score(labels, raw_proba)),
        "log_loss": float(log_loss(labels, calibrated_proba)),
    }


def get_project_root():
    """프로젝트 루트 경로 반환."""
    current = os.path.dirname(os.path.abspath(__file__))
    while current != "/":
        if os.path.exists(os.path.join(current, "src")):
            return current
        current = os.path.dirname(current)
    raise RuntimeError("프로젝트 루트를 찾을 수 없습니다")


def load_config(config_path):
    """config.yaml 로드."""
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def main(
    config_path: str = None,
    data_path: str = None,
    model_path: str = None,
    feature_columns_path: str = None,
    sampling_rate: float = 1.0,
    extra_features: Optional[Sequence[str]] = None,
):
    # extra_features: 학습이 prod 계약 뒤에 덧붙인 실험 피처(#405). 지정하면 계약
    # 검증이 "prod 접두부 정확 일치 + 나머지가 선언한 실험 피처"로 바뀐다. 미지정
    # (기본값)이면 기존의 엄격한 동등 검사 그대로다 — prod 경로는 느슨해지지 않는다.
    # sampling_rate: 학습 시 쓴 negative downsampling 실현 비율(#300). 다운샘플된
    # 분포로 학습된 모델의 출력 확률을 원분포로 보정해 LogLoss/Brier/calibration을
    # 올바른 분포에서 잰다. 기본 1.0 = 보정 없음(항등) — downsampling 미사용
    # 모델이나 standalone 평가의 하위호환 기본값(#300 결정 7).
    project_root = get_project_root()
    if config_path is None:
        config_path = os.path.join(project_root, "src", "pipeline", "config.yaml")
    elif not os.path.isabs(config_path):
        config_path = os.path.join(project_root, config_path)
    config = load_config(config_path)

    print("=" * 70)
    print("모델 평가")
    print("=" * 70)

    print("\n[Step 1] 모델 로드...")
    if model_path is None:
        model_path = os.path.join(project_root, config["artifacts"]["model_path"])
    elif not os.path.isabs(model_path):
        model_path = os.path.join(project_root, model_path)
    if feature_columns_path is None:
        feature_columns_path = os.path.join(project_root, config["artifacts"]["feature_columns_path"])
    elif not os.path.isabs(feature_columns_path):
        feature_columns_path = os.path.join(project_root, feature_columns_path)

    model = load_model(model_path)
    loaded_columns = load_feature_columns(feature_columns_path)
    if extra_features:
        feature_columns = require_experiment_feature_columns(
            loaded_columns, extra=extra_features
        )
    else:
        feature_columns = require_model_feature_columns(loaded_columns)

    print("\n[Step 2] 데이터 로드 (held-out test set)...")
    if data_path is None:
        data_path = os.path.join(project_root, config["artifacts"]["test_set_path"])
    elif not os.path.isabs(data_path):
        data_path = os.path.join(project_root, data_path)
    dataset = pd.read_csv(data_path)

    X = dataset[list(feature_columns)].copy()
    y = dataset["clicked"].copy()

    for column in CATEGORICAL_FEATURE_COLUMNS:
        X[column] = X[column].astype("category")

    print(f"  [OK] {len(dataset)} rows")

    print("\n[Step 3] 예측...")
    raw_pred_proba = model.predict_proba(X)[:, 1]
    # downsampling 보정(#300 결정 4). sampling_rate=1.0이면 항등(no-op).
    # 보정은 monotonic이라 ROC-AUC/PR-AUC/랭킹 지표는 불변이고, LogLoss/Brier/
    # calibration만 원분포 기준으로 이동한다(결정 5).
    y_pred_proba = apply_downsampling_calibration(raw_pred_proba, sampling_rate)
    if sampling_rate < 1.0:
        print(f"  [OK] 예측 완료 (downsampling 보정 적용, sampling_rate={sampling_rate})")
    else:
        print("  [OK] 예측 완료 (보정 없음)")

    print("\n[Step 4] 평가 지표 계산...")
    # AUC 계열은 순위 기반이라 보정 전/후 동일하므로 어느 확률로 재도 같다.
    roc_auc = evaluate_held_out_roc_auc(model, dataset, feature_columns)
    pr_auc = average_precision_score(y, y_pred_proba)
    # LogLoss/Brier는 보정된 확률(원분포 기준)로 잰다 — 보정 검증 근거(결정 5).
    logloss = log_loss(y, y_pred_proba)
    brier = brier_score_loss(y, y_pred_proba)

    print(f"  [OK] ROC-AUC: {roc_auc:.4f}  (보정에 불변)")
    print(f"  [OK] PR-AUC: {pr_auc:.4f}  (보정에 불변)")
    # 유저 단위 지표는 **전역 지표를 대체하지 않고 병기**한다(#505). 주 지표 교체는
    # 과거 실험과의 비교 가능성을 끊으므로 이 작업의 범위가 아니다(#493이 소유).
    # 패스스루 컬럼이 없는 과거 스냅샷은 조용히 건너뛴다 — 조립은 fail-closed지만
    # 평가까지 막으면 재현 평가 경로가 끊긴다.
    if GROUP_KEY_COLUMN in dataset.columns:
        grouped = grouped_roc_auc(y, y_pred_proba, dataset[GROUP_KEY_COLUMN])
        coverage = (
            f"(유저 {grouped.total_groups}명 중 {grouped.scored_groups}명 집계, "
            f"{grouped.skipped_groups}명 제외"
        )
        # 귀속 불가 행은 유저 수에 안 잡히므로 따로 보여야 커버리지가 정직해진다.
        coverage += (
            f", {GROUP_KEY_COLUMN} 결측 {grouped.null_key_rows}행 제외)"
            if grouped.null_key_rows
            else ")"
        )
        if grouped.value is None:
            print(
                f"  [OK] Grouped ROC-AUC({GROUP_KEY_COLUMN}): 계산 불가 {coverage} "
                "— 유저별로 양성·음성이 함께 있어야 계산됩니다"
            )
        else:
            print(
                f"  [OK] Grouped ROC-AUC({GROUP_KEY_COLUMN}): {grouped.value:.4f}  "
                f"{coverage}"
            )
    print(f"  [OK] Log Loss: {logloss:.4f}")
    print(f"  [OK] Brier: {brier:.4f}")
    # calibration 요약: 예측 평균 vs 실제 양성률(원분포 보정 후 서로 가까워야 함).
    print(
        f"  [OK] calibration: 예측 평균={float(y_pred_proba.mean()):.4f} "
        f"vs 실제 양성률={float(y.mean()):.4f}"
    )

    print("\n[Step 5] Baseline (LogisticRegression) 비교...")
    baseline_path = os.path.join(project_root, "models", "baseline.pkl")
    if os.path.exists(baseline_path):
        try:
            with open(baseline_path, "rb") as f:
                baseline_model = pickle.load(f)
            baseline_pred_proba = baseline_model.predict_proba(X)[:, 1]
            baseline_roc_auc = roc_auc_score(y, baseline_pred_proba)
            print(f"  [OK] Baseline ROC-AUC: {baseline_roc_auc:.4f}")
            print(f"  [OK] LightGBM vs Baseline: {roc_auc - baseline_roc_auc:+.4f}")
        except Exception as e:
            print(f"  [WARNING] Baseline 로드 실패: {e}")
    else:
        print(f"  [WARNING] Baseline 모델을 찾을 수 없음: {baseline_path}")

    print("\n" + "=" * 70)
    print("평가 완료")
    print("=" * 70)
    print(f"ROC-AUC: {roc_auc:.4f}")
    print(f"PR-AUC: {pr_auc:.4f}")
    print(f"Log Loss: {logloss:.4f}")


if __name__ == "__main__":
    main()
