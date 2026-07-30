#!/usr/bin/env python3
"""LightGBM 학습 파이프라인 Typer CLI.

[파이프라인] 피처 조립 → 학습 → 평가 → champion 승격 구간의 진입점(배선)을
담당한다: `python -m src.cli build-features / train-model / evaluate-model /
run-pipeline / promote-model`.

[기능] 각 단계 모듈에 인자를 전달하고 단계 순서를 정한다. run-pipeline은
build-features → train-model → evaluate-model 순서로 실행하며, registered model
버전 생성은 평가가 통과한 뒤에 수행한다(#421) — 평가가 실패하면 지표를 신뢰할
수 없는 후보 버전이 registry에 남지 않는다.

[비책임] 실제 조립·학습·평가·승격 로직은 각 모듈(src/pipeline/*.py,
src/tracking/promote.py)이 소유한다. DAG·스케줄·재시도는 인접 저장소
Autoresearch-airflow 소유다.
"""

import os
import sys
import traceback
from pathlib import Path
from typing import Optional

import typer

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.pipeline import build_training_dataset, train, evaluate  # noqa: E402
from src.tracking import promote  # noqa: E402
from src.tracking.promotion_result import (  # noqa: E402
    MODEL_PROMOTION_RESULT_CONTRACT,
    ModelPromotionResult,
    PromotionExecutionError,
    PromotionOutcome,
    PromotionReasonCode,
    write_result_file,
)

app = typer.Typer()


@app.command()
def build_features(
    output_path: Optional[str] = typer.Option(
        None, help="출력 CSV 경로 (기본: data/processed/training_dataset.csv)"
    ),
    events_start_date: Optional[str] = typer.Option(
        None, help="학습 기간 시작일 KST YYYY-MM-DD (spine=training_entity 조회)"
    ),
    events_end_date: Optional[str] = typer.Option(
        None, help="학습 기간 종료일 KST YYYY-MM-DD (포함)"
    ),
) -> None:
    """training_dataset.csv 생성 (offline feature store PIT 조회, #359 C2로 feast-only)."""
    build_training_dataset.main(
        output_path=output_path,
        events_start_date=events_start_date,
        events_end_date=events_end_date,
    )


@app.command()
def train_model(
    config_path: Optional[str] = typer.Option(None, help="config.yaml 경로 (기본: src/pipeline/config.yaml)"),
    data_path: Optional[str] = typer.Option(None, help="training dataset 경로 (config override)"),
    model_output: Optional[str] = typer.Option(None, help="모델 저장 경로 (config override)"),
    test_set_output: Optional[str] = typer.Option(
        None, help="Held-out test set 저장 경로 (config override, 병렬 실험 시 실험별로 분리 필요)"
    ),
    feature_columns_output: Optional[str] = typer.Option(None, help="Feature 목록 저장 경로 (config override)"),
    categorical_columns_output: Optional[str] = typer.Option(None, help="Categorical 카테고리 저장 경로 (config override)"),
    test_size: Optional[float] = typer.Option(None, help="Test set 비율 (config override)"),
    val_size: Optional[float] = typer.Option(None, help="Val set 비율 (config override)"),
    random_state: Optional[int] = typer.Option(None, help="Random state (config override, 데이터 split과 모델 둘 다 적용)"),
) -> None:
    """LightGBM 모델 훈련 (train/val/test 3-way split, test는 완전 held-out)."""
    train.main(
        config_path=config_path,
        data_path=data_path,
        model_output=model_output,
        test_set_output=test_set_output,
        feature_columns_output=feature_columns_output,
        categorical_columns_output=categorical_columns_output,
        test_size=test_size,
        val_size=val_size,
        random_state=random_state,
    )


@app.command()
def evaluate_model(
    config_path: Optional[str] = typer.Option(None, help="config.yaml 경로 (기본: src/pipeline/config.yaml)"),
    data_path: Optional[str] = typer.Option(None, help="평가용 데이터 경로 (config override, 기본: held-out test set)"),
    model_path: Optional[str] = typer.Option(None, help="모델 로드 경로 (config override)"),
    feature_columns_path: Optional[str] = typer.Option(None, help="Feature 목록 경로 (config override)"),
) -> None:
    """저장된 모델을 held-out test set으로 평가."""
    evaluate.main(
        config_path=config_path,
        data_path=data_path,
        model_path=model_path,
        feature_columns_path=feature_columns_path,
    )


@app.command()
def run_pipeline(
    dataset_path: Optional[str] = typer.Option(None, help="Training dataset 경로 (기본: data/processed/training_dataset.csv)"),
    events_start_date: Optional[str] = typer.Option(
        None, help="학습 기간 시작일 KST YYYY-MM-DD (spine=training_entity 조회)"
    ),
    events_end_date: Optional[str] = typer.Option(
        None, help="학습 기간 종료일 KST YYYY-MM-DD (포함)"
    ),
    config_path: Optional[str] = typer.Option(None, help="config.yaml 경로 (기본: src/pipeline/config.yaml)"),
    model_output: Optional[str] = typer.Option(None, help="모델 저장 경로 (config override)"),
    test_set_output: Optional[str] = typer.Option(
        None, help="Held-out test set 저장 경로 (config override, 병렬 실험 시 실험별로 분리 필요)"
    ),
    feature_columns_output: Optional[str] = typer.Option(None, help="Feature 목록 저장 경로 (config override)"),
    categorical_columns_output: Optional[str] = typer.Option(None, help="Categorical 카테고리 저장 경로 (config override)"),
    test_size: Optional[float] = typer.Option(None, help="Test set 비율 (config override)"),
    val_size: Optional[float] = typer.Option(None, help="Val set 비율 (config override)"),
    random_state: Optional[int] = typer.Option(None, help="Random state (config override, 데이터 split과 모델 둘 다 적용)"),
) -> None:
    """전체 파이프라인 실행: build-features -> train-model -> evaluate-model -> 등록.

    등록(Model Registry 버전 생성)은 평가 통과 뒤에만 수행하는 별도 단계다(#421).
    조립 경로는 #359 C2로 feast-only다.
    """
    typer.echo("=" * 70)
    typer.echo("전체 파이프라인 실행")
    typer.echo("=" * 70)

    typer.echo("\n[1/4] build-features 실행...")
    build_training_dataset.main(
        output_path=dataset_path,
        events_start_date=events_start_date,
        events_end_date=events_end_date,
    )

    # 어떤 기간·소스로 학습했는지 MLflow run에 lineage로 남긴다(#359). C2로 조립 경로는
    # feast(offline store PIT)가 유일하므로 FeatureService·registry·기간을 기록한다.
    from src.features.feast_retrieval import DEFAULT_SERVICE

    # run-pipeline은 C2로 feast-only다. 위 build-features(_assemble_via_feast)가
    # events 기간을 필수로 검증하고 GCS_REGISTRY_PATH를 필수로 읽으므로(미설정이면
    # 여기 도달 전에 멈춤), 이 시점엔 셋 다 항상 존재한다. 따라서 조립이 필수로 읽는
    # 값을 lineage는 "있으면 기록"으로 두던 비대칭을 없애고 무조건 기록해, registry나
    # 기간이 빠진 재현 불가 run이 남지 않게 한다(#359 C2 리뷰).
    data_source_params = {
        "assembly_source": "feast",
        "feature_service": DEFAULT_SERVICE,
        "events_start_date": events_start_date,
        "events_end_date": events_end_date,
        "feast_registry_path": os.environ["GCS_REGISTRY_PATH"],
    }

    typer.echo("\n[2/4] train-model 실행...")
    # train.main은 실현 sampling_rate(#300)를 담은 TrainingOutcome을 반환한다 —
    # evaluate가 오프라인 지표(LogLoss/calibration)를 원분포 기준으로 재도록 넘긴다.
    # defer_registration=True: registered model 버전 생성만 평가 뒤로 미룬다(#421).
    # run 로깅(파라미터·메트릭·아티팩트)은 학습 시점에 그대로 남는다.
    outcome = train.main(
        config_path=config_path,
        data_path=dataset_path,
        model_output=model_output,
        test_set_output=test_set_output,
        feature_columns_output=feature_columns_output,
        categorical_columns_output=categorical_columns_output,
        test_size=test_size,
        val_size=val_size,
        random_state=random_state,
        extra_params=data_source_params,
        defer_registration=True,
    )

    # dataset_path(방금 만든 train+val+test 전체)는 넘기지 않는다: evaluate는
    # train-model이 분리해 저장한 held-out test set으로만 채점해야 하며, 그대로
    # 넘기면 data leakage가 재발한다. 대신 test_set_output/feature_columns_output을
    # 그대로 전달해서, 병렬로 여러 run-pipeline을 돌릴 때도(각자 다른 경로를 줬다면)
    # 자기 자신이 만든 test set/feature 목록으로 채점되도록 짝을 맞춘다.
    typer.echo("\n[3/4] evaluate-model 실행...")
    evaluate.main(
        config_path=config_path,
        data_path=test_set_output,
        model_path=model_output,
        feature_columns_path=feature_columns_output,
        sampling_rate=outcome.sampling_rate,
    )

    # 평가가 통과한 뒤에야 registered model 버전을 만든다(#421). 평가가 실패하면
    # evaluate.main의 예외가 여기까지 오지 않으므로, 지표를 신뢰할 수 없는 후보
    # 버전이 registry에 쌓이지 않는다. 학습 run과 아티팩트는 이미 남아 있어,
    # 데이터를 고친 뒤 재학습하면 정상 후보가 다시 만들어진다.
    if outcome.pending_registration is not None:
        typer.echo("\n[등록] 평가 통과 — Model Registry 등록...")
        train.register_pending_model(outcome.pending_registration)

    typer.echo("\n" + "=" * 70)
    typer.echo("파이프라인 완료")
    typer.echo("=" * 70)


@app.command()
def promote_model(
    model_name: str = typer.Option("ctr-model", help="Registry에 등록된 main 모델 이름"),
    champion_alias: str = typer.Option("champion", help="승격 대상 alias"),
    calibration_model_name: str = typer.Option(
        "ctr-calibration-model",
        help="[DEPRECATED · 무시됨] #390에서 calibration은 main run에 종속돼 별도 등록하지 않습니다. "
        "호출 계약 하위호환을 위해 인자만 남겨두며 값은 사용하지 않습니다.",
    ),
    result_contract: Optional[str] = typer.Option(
        None,
        "--result-contract",
        help="구조화 결과 계약. --result-path와 함께 model-promotion-result-v1만 허용합니다.",
    ),
    result_path: Optional[Path] = typer.Option(
        None,
        "--result-path",
        help="구조화 결과 JSON 파일 경로. --result-contract와 함께 지정합니다.",
    ),
) -> None:
    """게이트(지표 비교 + downsampling calibration 아티팩트 존재) 통과 시 신규 후보를 champion으로 승격.

    calibration_model_name은 #390에서 무시된다(deprecated). Airflow DAG(Autoresearch-airflow#137)가
    아직 이 플래그를 넘기더라도 기동이 깨지지 않도록 인자 표면만 유지하며, DAG에서 플래그를 제거한
    뒤 후속 PR로 이 인자를 걷어낸다.
    """
    structured_mode_requested = result_contract is not None or result_path is not None
    structured_mode_valid = (
        result_contract == MODEL_PROMOTION_RESULT_CONTRACT
        and result_path is not None
    )
    if structured_mode_requested and not structured_mode_valid:
        typer.echo(
            "[인자 오류] --result-contract와 --result-path를 함께 지정하고 "
            f"--result-contract={MODEL_PROMOTION_RESULT_CONTRACT}을 사용해 주세요.",
            err=True,
        )
        raise typer.Exit(code=2)

    # 기본값과 다른 값이 명시적으로 넘어오면 stderr에 deprecation 경고를 남긴다 — DAG가
    # 기본값과 같은 문자열을 넘기면 감지 못하지만(한계), 다른 값이면 "아직 호출부가 이 플래그를
    # 쓰고 있다"는 신호를 로그로 남겨 언제 걷어내도 되는지 추적하게 한다(#395 리뷰).
    if calibration_model_name != "ctr-calibration-model":
        typer.echo(
            "[경고] --calibration-model-name은 #390에서 무시됩니다(deprecated). "
            "호출부(DAG)에서 이 플래그를 제거해 주세요.",
            err=True,
        )
    if structured_mode_valid:
        _run_structured_promotion(
            model_name=model_name,
            champion_alias=champion_alias,
            result_path=result_path,
        )
        return

    _run_legacy_promotion(
        model_name=model_name,
        champion_alias=champion_alias,
    )


def _error_result(
    *,
    model_name: str,
    champion_alias: str,
    reason_code: PromotionReasonCode,
    candidate_version: Optional[str] = None,
    champion_version: Optional[str] = None,
    candidate_metric: Optional[float] = None,
    champion_metric: Optional[float] = None,
) -> ModelPromotionResult:
    """외부 예외 내용을 포함하지 않는 구조화 오류 결과를 만든다."""
    return ModelPromotionResult(
        outcome=PromotionOutcome.ERROR,
        model_name=model_name,
        champion_alias=champion_alias,
        candidate_version=candidate_version,
        champion_version=champion_version,
        candidate_metric=candidate_metric,
        champion_metric=champion_metric,
        reason_code=reason_code,
    )


def _emit_structured_error_diagnostic(
    *,
    reason_code: PromotionReasonCode,
    error: BaseException,
    include_stack: bool,
) -> None:
    """비밀 가능성이 있는 예외 메시지 없이 안전한 stderr 진단을 출력한다."""
    typer.echo(
        f"[구조화 결과 오류] reason_code={reason_code.value} "
        f"error_type={type(error).__name__}",
        err=True,
    )
    if include_stack and error.__traceback__ is not None:
        for frame in traceback.extract_tb(error.__traceback__):
            typer.echo(
                f"  at {frame.filename}:{frame.lineno} in {frame.name}",
                err=True,
            )


def _run_structured_promotion(
    *,
    model_name: str,
    champion_alias: str,
    result_path: Path,
) -> None:
    """구조화 결과를 파일과 stdout에 기록하고 오류에만 non-zero로 종료한다."""
    try:
        result = promote.main(
            model_name=model_name,
            champion_alias=champion_alias,
        )
    except PromotionExecutionError as exc:
        _emit_structured_error_diagnostic(
            reason_code=exc.reason_code,
            error=exc,
            include_stack=False,
        )
        result = _error_result(
            model_name=model_name,
            champion_alias=champion_alias,
            reason_code=exc.reason_code,
            candidate_version=exc.candidate_version,
            champion_version=exc.champion_version,
            candidate_metric=exc.candidate_metric,
            champion_metric=exc.champion_metric,
        )
    except Exception as exc:
        _emit_structured_error_diagnostic(
            reason_code=PromotionReasonCode.UNEXPECTED_ERROR,
            error=exc,
            include_stack=True,
        )
        result = _error_result(
            model_name=model_name,
            champion_alias=champion_alias,
            reason_code=PromotionReasonCode.UNEXPECTED_ERROR,
        )

    try:
        write_result_file(result, result_path)
    except Exception as exc:
        _emit_structured_error_diagnostic(
            reason_code=PromotionReasonCode.RESULT_WRITE_FAILED,
            error=exc,
            include_stack=False,
        )
        result = _error_result(
            model_name=model_name,
            champion_alias=champion_alias,
            reason_code=PromotionReasonCode.RESULT_WRITE_FAILED,
            candidate_version=result.candidate_version,
            champion_version=result.champion_version,
            candidate_metric=result.candidate_metric,
            champion_metric=result.champion_metric,
        )

    typer.echo(result.model_dump_json())
    if result.outcome is PromotionOutcome.ERROR:
        raise typer.Exit(code=1)


def _run_legacy_promotion(
    *,
    model_name: str,
    champion_alias: str,
) -> None:
    """구조화 opt-in 전 호출부의 메시지와 exit code 계약을 보존한다."""
    try:
        result = promote.main(
            model_name=model_name,
            champion_alias=champion_alias,
        )
    except promote.GateRejectedError as exc:
        typer.echo(f"[게이트 미달] {exc}", err=True)
        raise typer.Exit(code=1)
    except Exception as exc:
        typer.echo(f"[에러] promote-model 실행 중 오류: {exc}", err=True)
        raise typer.Exit(code=1)

    if result.outcome is PromotionOutcome.REJECTED:
        if result.legacy_message is not None:
            detail = result.legacy_message
        elif result.reason_code is PromotionReasonCode.METRIC_BELOW_CHAMPION:
            detail = (
                f"게이트1 미달: 후보 {result.model_name} "
                f"v{result.candidate_version} "
                f"val_roc_auc={result.candidate_metric:.4f} < "
                f"champion({result.champion_alias}) "
                f"val_roc_auc={result.champion_metric:.4f}"
            )
        elif result.reason_code is PromotionReasonCode.CALIBRATION_ARTIFACT_MISSING:
            detail = (
                f"게이트2 미달: 후보 {result.model_name} "
                f"v{result.candidate_version}에 필요한 calibration "
                "아티팩트가 없습니다."
            )
        else:
            detail = (
                f"후보 {result.model_name} v{result.candidate_version}: "
                "서빙 calibration 준비가 완료되지 않았습니다."
            )
        typer.echo(f"[게이트 미달] {detail}", err=True)
        raise typer.Exit(code=1)
    if result.outcome is PromotionOutcome.NO_CANDIDATE:
        typer.echo(f"{model_name}: 평가할 신규 후보 버전 없음 — no-op")
        return
    if result.outcome is PromotionOutcome.ERROR:
        typer.echo(f"[에러] promote-model 실행 실패: {result.reason_code.value}", err=True)
        raise typer.Exit(code=1)

    typer.echo(
        f"[OK] {model_name} v{result.candidate_version} "
        f"-> @{champion_alias} 승격 완료"
    )


if __name__ == "__main__":
    app()
