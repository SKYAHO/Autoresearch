#!/usr/bin/env python3
"""
모델 훈련 스크립트.

config.yaml의 설정을 읽어 LightGBM 모델을 훈련하고 저장한다.
"""

import os
import sys
import yaml
import pandas as pd
from sklearn.model_selection import train_test_split
from sklearn.metrics import roc_auc_score

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

import mlflow  # noqa: E402

from src.models.lgbm_model import LGBMModel  # noqa: E402
from src.models.downsampling import downsample_negatives  # noqa: E402
from src.models.calibration import (  # noqa: E402
    CALIBRATION_PARAM_FILENAME,
    DownsamplingCalibrator,
)
from src.features.model_contract import (  # noqa: E402
    CATEGORICAL_FEATURE_COLUMNS,
    MODEL_FEATURE_COLUMNS,
)
from src.utils.model_utils import save_model, save_feature_columns, save_categorical_columns  # noqa: E402
from src.tracking.client import get_or_create_experiment, set_tracking_uri  # noqa: E402
from src.tracking.logger import log_artifact, log_metrics, log_parameters  # noqa: E402
from src.tracking.registry import register_model  # noqa: E402


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


def collect_categorical_categories(
    X_train: pd.DataFrame, X_val: pd.DataFrame, categorical_columns: list
) -> dict:
    """
    Categorical 컬럼을 train/val union 카테고리로 캐스팅하고 카테고리 목록을 반환.

    반환된 dict는 categorical_columns.pkl 아티팩트로 저장되어 서빙이 학습과
    동일한 category 코드 매핑을 재현하는 데 사용된다 (src/serving/model_loader.py).
    """
    categories_by_column: dict = {}
    for col in categorical_columns:
        if col not in X_train.columns:
            continue
        categories = pd.api.types.union_categoricals(
            [X_train[col].astype("category"), X_val[col].astype("category")]
        ).categories
        X_train[col] = pd.Categorical(X_train[col], categories=categories)
        X_val[col] = pd.Categorical(X_val[col], categories=categories)
        categories_by_column[col] = categories.tolist()
    return categories_by_column


def main(
    config_path: str = None,
    data_path: str = None,
    model_output: str = None,
    test_set_output: str = None,
    feature_columns_output: str = None,
    categorical_columns_output: str = None,
    test_size: float = None,
    val_size: float = None,
    random_state: int = None,
    extra_params: dict = None,
):
    project_root = get_project_root()
    if config_path is None:
        config_path = os.path.join(project_root, "src", "pipeline", "config.yaml")
    elif not os.path.isabs(config_path):
        config_path = os.path.join(project_root, config_path)
    config = load_config(config_path)

    # MLflow 초기화
    tracking_uri = os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5000")
    set_tracking_uri(tracking_uri)
    experiment_id = get_or_create_experiment("ctr-model-training")

    print("=" * 70)
    print("LightGBM 모델 훈련")
    print("=" * 70)

    with mlflow.start_run(experiment_id=experiment_id) as run:
        print("\n[Step 1] 데이터 로드...")
        if data_path is None:
            data_path = os.path.join(project_root, config["data"]["path"])
        elif not os.path.isabs(data_path):
            data_path = os.path.join(project_root, data_path)
        dataset = pd.read_csv(data_path)
        print(f"  [OK] {len(dataset)} rows, {len(dataset.columns)} columns")

        print("\n[Step 2] Train/Val/Test 분할 (Test는 완전 held-out)...")
        if test_size is None:
            test_size = config["data"]["test_size"]
        if val_size is None:
            val_size = config["data"]["val_size"]
        if random_state is None:
            random_state = config["data"]["random_state"]

        if test_size + val_size >= 1:
            raise ValueError(
                f"test_size({test_size}) + val_size({val_size}) >= 1 입니다 — "
                "train에 데이터가 남지 않거나 분할 자체가 불가능합니다. "
                "두 값의 합이 1보다 작아야 합니다 (예: test_size=0.2, val_size=0.2)."
            )

        train_val_df, test_df = train_test_split(
            dataset,
            test_size=test_size,
            random_state=random_state,
            stratify=dataset["clicked"],
        )
        val_ratio_within_train_val = val_size / (1 - test_size)
        train_df, val_df = train_test_split(
            train_val_df,
            test_size=val_ratio_within_train_val,
            random_state=random_state,
            stratify=train_val_df["clicked"],
        )
        print(f"  [OK] Train: {len(train_df)}, Val: {len(val_df)}, Test: {len(test_df)} (Test는 학습에 미사용)")

        if test_set_output is None:
            test_set_path = os.path.join(project_root, config["artifacts"]["test_set_path"])
        elif not os.path.isabs(test_set_output):
            test_set_path = os.path.join(project_root, test_set_output)
        else:
            test_set_path = test_set_output
        os.makedirs(os.path.dirname(test_set_path), exist_ok=True)
        test_df.to_csv(test_set_path, index=False)
        print(f"  [저장] Test set (held-out): {test_set_path}")

        print("\n[Step 3] Feature/Label 분리...")
        feature_columns = list(MODEL_FEATURE_COLUMNS)
        categorical_columns = list(CATEGORICAL_FEATURE_COLUMNS)

        X_train = train_df[feature_columns].copy()
        y_train = train_df["clicked"].copy()
        X_val = val_df[feature_columns].copy()
        y_val = val_df["clicked"].copy()

        print(f"  [OK] Train features: {X_train.shape}, ratio={y_train.mean():.3%}")
        print(f"  [OK] Val features: {X_val.shape}, ratio={y_val.mean():.3%}")

        # negative downsampling — train split에만 적용한다(#300). val/test는 위에서
        # 원분포로 유지된다. sampling_rate=1.0이면 downsample_negatives가 원본 그대로
        # 반환(no-op)한다. realized_sampling_rate는 라운딩으로 nominal과 미세하게
        # 다를 수 있어 실현값을 이후 보정·기록에 쓴다.
        nominal_sampling_rate = float(config["model"].get("sampling_rate", 1.0))
        realized_sampling_rate = 1.0
        if nominal_sampling_rate < 1.0:
            print("\n[Step 3b] Negative downsampling (train split only)...")
            n_train_before = len(y_train)
            X_train, y_train, realized_sampling_rate = downsample_negatives(
                X_train, y_train, nominal_sampling_rate, random_state=random_state
            )
            print(
                f"  [OK] {n_train_before} → {len(y_train)} rows "
                f"(sampling_rate nominal={nominal_sampling_rate}, "
                f"realized={realized_sampling_rate:.4f}), ratio={y_train.mean():.3%}"
            )

        print("\n[Step 4] Categorical 컬럼 dtype 변환...")
        categories_by_column = collect_categorical_categories(
            X_train, X_val, categorical_columns
        )
        print(f"  [OK] {len(categorical_columns)} categorical columns 설정")

        print("\n[Step 5] scale_pos_weight 계산...")
        configured_spw = config["model"]["scale_pos_weight"]
        # downsampling과 scale_pos_weight를 둘 다 걸면 이중 보정이라 He 보정 공식의
        # 전제가 깨진다(#300 결정 6). downsampling이 켜지면 scale_pos_weight는
        # 1로 대체(강제)한다. 단 누군가 config에 명시적 숫자값(≠1, "auto" 아님)을
        # downsampling과 함께 세팅했다면 의도 충돌이므로 fail-closed로 막는다.
        # 이 강제는 auto 계산 이전에 수행한다 — 순서가 뒤바뀌면 auto가 계산한 큰
        # 값이 강제(=1)를 덮어써 이중 보정이 그대로 남는다.
        # LightGBM엔 is_unbalance라는 또 다른 자동 밸런싱 옵션이 있으나 현재
        # config/lgbm_model.py 어디에도 없다(비활성). 추가되면 여기 가드를 확장한다.
        # "downsampling 모드" 판단은 Step 3b 활성화와 동일하게 nominal 기준으로
        # 통일한다. realized로 판단하면 negative가 0/극소라 realized==1.0으로
        # 떨어지는 경우 이 강제를 건너뛰고 auto 경로로 빠져 neg_count=0 →
        # scale_pos_weight=0/pos=0(무효값)이 되는 divergence가 생긴다.
        if nominal_sampling_rate < 1.0:
            explicit_numeric = configured_spw != "auto" and float(configured_spw) != 1
            if explicit_numeric:
                raise ValueError(
                    "downsampling(sampling_rate<1.0)과 scale_pos_weight="
                    f"{configured_spw}를 함께 세팅했습니다 — 이중 보정입니다. "
                    "downsampling이 scale_pos_weight를 대체하므로 둘 중 하나만 쓰세요"
                    "(#300 결정 6)."
                )
            scale_pos_weight = 1
            print("  [OK] downsampling 활성 → scale_pos_weight=1 강제(이중 보정 방지)")
        elif configured_spw == "auto":
            neg_count = (y_train == 0).sum()
            pos_count = (y_train == 1).sum()
            scale_pos_weight = neg_count / pos_count
            print(f"  [OK] auto 계산: neg={neg_count}, pos={pos_count}, ratio={scale_pos_weight:.2f}")
        else:
            scale_pos_weight = configured_spw
            print(f"  [OK] 고정값: {scale_pos_weight}")

        params = {
            "model_type": "LightGBM",
            "n_estimators": config["model"]["n_estimators"],
            "learning_rate": config["model"]["learning_rate"],
            "num_leaves": config["model"]["num_leaves"],
            "scale_pos_weight": scale_pos_weight,
            "random_state": random_state,
            # downsampling 실현 비율(#300). 1.0이면 downsampling 미적용. 서빙 보정은
            # 이 값을 쓰므로 실현값(nominal 아님)을 기록한다.
            "sampling_rate": realized_sampling_rate,
            "train_size": len(train_df),
            "val_size": len(val_df),
            "test_size": len(test_df),
        }
        if extra_params:
            # 데이터 소스 계보(예: events_source/events_start_date/events_end_date)를
            # run에 남겨서, 어떤 기간의 데이터로 학습했는지 항상 조회 가능하게 한다.
            params.update(extra_params)
        log_parameters(params)

        print("\n[Step 6] LightGBM 모델 훈련...")
        model = LGBMModel(
            scale_pos_weight=scale_pos_weight,
            n_estimators=config["model"]["n_estimators"],
            learning_rate=config["model"]["learning_rate"],
            num_leaves=config["model"]["num_leaves"],
            random_state=random_state,
        )
        model.fit(X_train, y_train, categorical_features=categorical_columns)
        print("  [OK] 훈련 완료")

        print("\n[Step 7] 검증...")
        y_val_pred_proba = model.predict_proba(X_val)[:, 1]
        val_roc_auc = roc_auc_score(y_val, y_val_pred_proba)
        print(f"  [OK] Val ROC-AUC: {val_roc_auc:.4f}")

        log_metrics(
            {
                "val_roc_auc": val_roc_auc,
                "train_positive_ratio": float(y_train.mean()),
                "val_positive_ratio": float(y_val.mean()),
            }
        )

        if (X_val["historical_category_match"] == 1).sum() == 0:
            print("  ⚠️  historical_category_match에 1이 없음 (dtype 불일치 가능성)")

        print("\n[Step 8] 모델 저장...")
        if model_output is None:
            model_path = os.path.join(project_root, config["artifacts"]["model_path"])
        elif not os.path.isabs(model_output):
            model_path = os.path.join(project_root, model_output)
        else:
            model_path = model_output
        if feature_columns_output is None:
            feature_columns_path = os.path.join(project_root, config["artifacts"]["feature_columns_path"])
        elif not os.path.isabs(feature_columns_output):
            feature_columns_path = os.path.join(project_root, feature_columns_output)
        else:
            feature_columns_path = feature_columns_output
        if categorical_columns_output is None:
            categorical_columns_path = os.path.join(
                project_root, config["artifacts"]["categorical_columns_path"]
            )
        elif not os.path.isabs(categorical_columns_output):
            categorical_columns_path = os.path.join(project_root, categorical_columns_output)
        else:
            categorical_columns_path = categorical_columns_output

        save_model(model.model, model_path)
        save_feature_columns(feature_columns, feature_columns_path)
        save_categorical_columns(categories_by_column, categorical_columns_path)

        # artifact 경로(model/, features/)는 서빙 로더(src/serving/model_loader.py)의
        # MLflow 다운로드 경로 상수와 계약이다 — 변경 시 양쪽을 함께 갱신한다.
        log_artifact(local_path=model_path, artifact_path="model")
        log_artifact(local_path=feature_columns_path, artifact_path="features")
        log_artifact(local_path=categorical_columns_path, artifact_path="features")

        print("\n[Step 9] Model Registry 등록...")
        model_name = config["registry"]["model_name"]
        # log_artifact(..., artifact_path="model")과 짝을 맞춰야 한다 — 서빙 로더의
        # MLFLOW_MODEL_ARTIFACT_PATH 상수(model/lgbm_model.joblib)도 같은
        # "model/" 아티팩트 경로 아래 파일을 참조한다.
        model_uri = f"runs:/{run.info.run_id}/model"
        # sampling_rate를 모델 버전 tag로도 기록한다(#300 결정 7). 서빙이 alias로
        # 모델 버전을 로드하는 순간 tag에서 직접 읽어(run→param 간접 조회 없이)
        # 로드 시 1회 캐싱한다. 승격 게이트(set_model_alias)도 이 tag를 본다.
        registry_tags = {
            "val_roc_auc": f"{val_roc_auc:.4f}",
            "sampling_rate": f"{realized_sampling_rate}",
        }
        if extra_params:
            registry_tags.update({k: str(v) for k, v in extra_params.items()})
        # 등록 실패로 이미 끝난 학습 run을 FAILED 처리하지 않는다(best-effort).
        registered_version = None
        try:
            registered_version = register_model(model_uri, model_name, tags=registry_tags)
            print(f"  [OK] {model_name} v{registered_version} 등록 완료")
        except Exception as exc:
            print(f"  ⚠️  Model Registry 등록 실패 — 학습 결과(모델·아티팩트)는 정상 저장됨: {exc}")

        # calibration을 별도 아티팩트(JSON w) + 별도 등록 모델로 패키징한다(#302).
        # 배포 단위를 "메인 + calibration" 2개로 만들어 서빙이 main→calibration으로
        # 체이닝한다. 수식은 상수(He 2014)지만 멘토 요구(14차 코칭)대로 물리적으로 분리된
        # 모델로 등록한다. downsampling 미사용(w=1.0)이면 보정할 게 없어 생략한다(하위호환).
        calibration_model_name = config["registry"].get(
            "calibration_model_name", "ctr-calibration-model"
        )
        calibration_version = None
        if realized_sampling_rate < 1.0:
            calibration_path = os.path.join(
                os.path.dirname(model_path), CALIBRATION_PARAM_FILENAME
            )
            DownsamplingCalibrator(realized_sampling_rate).save(calibration_path)
            # 서빙 로더의 MLFLOW_CALIBRATION_ARTIFACT_PATH(calibration/calibration.json)와 계약.
            log_artifact(local_path=calibration_path, artifact_path="calibration")
            # 짝 식별: calibration 버전에 main run_id를 tag로 남겨, 서빙이 두 alias를
            # resolve할 때 맞는 조합인지 fail-closed로 검증하게 한다(model_loader 페어링 검증).
            calibration_tags = {
                "sampling_rate": f"{realized_sampling_rate}",
                "main_run_id": run.info.run_id,
            }
            try:
                calibration_version = register_model(
                    f"runs:/{run.info.run_id}/calibration",
                    calibration_model_name,
                    tags=calibration_tags,
                )
                print(f"  [OK] {calibration_model_name} v{calibration_version} 등록 완료")
            except Exception as exc:
                print(f"  ⚠️  calibration 모델 등록 실패 — 학습 결과는 정상 저장됨: {exc}")

    print("\n" + "=" * 70)
    print("훈련 완료")
    print("=" * 70)
    print(f"Val ROC-AUC: {val_roc_auc:.4f}")
    print(f"Model: {model_path}")
    print(f"Feature columns: {feature_columns_path}")
    print(f"Categorical columns: {categorical_columns_path}")
    print(
        f"Registered model: {model_name} v{registered_version}"
        if registered_version is not None
        else f"Registered model: 등록 실패 (건너뜀 — 위 경고 로그 참고, run_id={run.info.run_id})"
    )

    # 실현 sampling_rate를 반환한다 — run-pipeline이 evaluate에 넘겨 오프라인
    # 지표(LogLoss/calibration)를 원분포 기준으로 재게 한다(#300 결정 4).
    return realized_sampling_rate


if __name__ == "__main__":
    main()
