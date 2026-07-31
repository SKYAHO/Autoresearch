from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import typer
from typer.testing import CliRunner

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src import cli  # noqa: E402
from src.pipeline import train as train_module  # noqa: E402
from src.tracking.promotion_result import (  # noqa: E402
    ModelPromotionResult,
    PromotionExecutionError,
    PromotionOutcome,
    PromotionReasonCode,
)


def _promotion_result(
    outcome: PromotionOutcome,
    reason_code: PromotionReasonCode,
) -> ModelPromotionResult:
    return ModelPromotionResult(
        outcome=outcome,
        model_name="ctr-model",
        champion_alias="champion",
        candidate_version="4",
        champion_version="3",
        candidate_metric=0.80,
        champion_metric=0.75,
        reason_code=reason_code,
    )


def test_run_pipeline_forwards_dates_to_build_features(monkeypatch):
    build_features_call = {}
    # build-features 성공 뒤 lineage가 GCS_REGISTRY_PATH를 필수로 읽는다(#359 C2, 무조건 기록).
    monkeypatch.setenv("GCS_REGISTRY_PATH", "gs://fake/registry.db")
    monkeypatch.setattr(cli.build_training_dataset, "main", lambda **kw: build_features_call.update(kw))
    monkeypatch.setattr(cli.train, "main", lambda **kw: _pipeline_outcome())
    monkeypatch.setattr(cli.evaluate, "main", MagicMock())
    monkeypatch.setattr(cli.train, "register_pending_model", MagicMock())

    cli.run_pipeline(
        dataset_path="dataset.csv",
        events_start_date="2026-07-01",
        events_end_date="2026-07-08",
        config_path=None,
        model_output=None,
        test_set_output="test_set.csv",
        feature_columns_output="feature_columns.json",
        categorical_columns_output=None,
        test_size=None,
        val_size=None,
        random_state=None,
        extra_features=None,
        experiment=None,
    )

    # C2로 feast-only: build-features에 output_path + 기간만 넘긴다(duckdb 인자 없음).
    assert build_features_call == {
        "output_path": "dataset.csv",
        "events_start_date": "2026-07-01",
        "events_end_date": "2026-07-08",
    }


def test_train_model_forwards_explicit_seed_triplet(monkeypatch):
    train_call = {}
    monkeypatch.setattr(
        cli.train, "main", lambda **kwargs: train_call.update(kwargs)
    )

    cli.train_model(
        config_path=None,
        data_path=None,
        model_output=None,
        test_set_output=None,
        feature_columns_output=None,
        categorical_columns_output=None,
        test_size=None,
        val_size=None,
        random_state=None,
        split_seed=11,
        model_seed=12,
        sampler_seed=13,
        extra_features=None,
        experiment=None,
    )

    assert (
        train_call["split_seed"],
        train_call["model_seed"],
        train_call["sampler_seed"],
    ) == (11, 12, 13)
    assert "require_snapshot" not in train_call


def test_run_pipeline_requires_verified_snapshot_and_forwards_seed_triplet(monkeypatch):
    train_call = {}
    monkeypatch.setenv("GCS_REGISTRY_PATH", "gs://fake/registry.db")
    monkeypatch.setattr(cli.build_training_dataset, "main", MagicMock())
    monkeypatch.setattr(
        cli.train,
        "main",
        lambda **kwargs: train_call.update(kwargs) or _pipeline_outcome(),
    )
    monkeypatch.setattr(cli.evaluate, "main", MagicMock())
    monkeypatch.setattr(cli.train, "register_pending_model", MagicMock())

    cli.run_pipeline(
        dataset_path=None,
        events_start_date="2026-07-01",
        events_end_date="2026-07-08",
        config_path=None,
        model_output=None,
        test_set_output=None,
        feature_columns_output=None,
        categorical_columns_output=None,
        test_size=None,
        val_size=None,
        random_state=None,
        split_seed=11,
        model_seed=12,
        sampler_seed=13,
        extra_features=None,
        experiment=None,
    )

    assert train_call["require_snapshot"] is True
    assert (
        train_call["split_seed"],
        train_call["model_seed"],
        train_call["sampler_seed"],
    ) == (11, 12, 13)


def test_run_pipeline_logs_feast_lineage_as_train_extra_params(monkeypatch):
    from src.features.feast_retrieval import DEFAULT_SERVICE

    train_call = {}
    monkeypatch.setenv("GCS_REGISTRY_PATH", "gs://fake/registry.db")
    monkeypatch.setattr(cli.build_training_dataset, "main", MagicMock())
    monkeypatch.setattr(
        cli.train, "main", lambda **kw: train_call.update(kw) or _pipeline_outcome()
    )
    monkeypatch.setattr(cli.evaluate, "main", MagicMock())
    monkeypatch.setattr(cli.train, "register_pending_model", MagicMock())

    cli.run_pipeline(
        dataset_path=None,
        events_start_date="2026-07-01",
        events_end_date="2026-07-08",
        config_path=None,
        model_output=None,
        test_set_output=None,
        feature_columns_output=None,
        categorical_columns_output=None,
        test_size=None,
        val_size=None,
        random_state=None,
        extra_features=None,
        experiment=None,
    )

    # feast-only lineage: assembly_source=feast + FeatureService + registry + 기간.
    assert train_call["extra_params"] == {
        "assembly_source": "feast",
        "feature_service": DEFAULT_SERVICE,
        "events_start_date": "2026-07-01",
        "events_end_date": "2026-07-08",
        "feast_registry_path": "gs://fake/registry.db",
    }


def _pipeline_outcome():
    """run-pipeline이 train.main에서 받는 반환값(#421)."""
    return cli.train.TrainingOutcome(
        sampling_rate=1.0,
        run_id="run-1",
        registered_version=None,
        pending_registration=cli.train.PendingRegistration(
            model_uri="runs:/run-1/model", model_name="ctr-model", tags={"val_roc_auc": "0.7"}
        ),
    )


def test_run_pipeline_registers_model_only_after_evaluation(monkeypatch):
    """#421: registered model 버전 생성은 evaluate 성공 뒤에 일어나야 한다."""
    calls = []
    monkeypatch.setenv("GCS_REGISTRY_PATH", "gs://fake/registry.db")
    monkeypatch.setattr(cli.build_training_dataset, "main", MagicMock())

    def _fake_train(**kwargs):
        calls.append(("train", kwargs.get("defer_registration")))
        return _pipeline_outcome()

    monkeypatch.setattr(cli.train, "main", _fake_train)
    monkeypatch.setattr(cli.evaluate, "main", lambda **kw: calls.append(("evaluate", None)))
    monkeypatch.setattr(
        cli.train,
        "register_pending_model",
        lambda pending: calls.append(("register", pending.model_name)) or "7",
    )

    cli.run_pipeline(
        dataset_path=None,
        events_start_date="2026-07-01",
        events_end_date="2026-07-08",
        config_path=None,
        model_output=None,
        test_set_output=None,
        feature_columns_output=None,
        categorical_columns_output=None,
        test_size=None,
        val_size=None,
        random_state=None,
        extra_features=None,
        experiment=None,
    )

    assert [step for step, _ in calls] == ["train", "evaluate", "register"]
    # train 단계에서는 등록을 보류하도록 요청해야 한다.
    assert calls[0][1] is True
    assert calls[2][1] == "ctr-model"


def test_run_pipeline_skips_registration_when_evaluation_fails(monkeypatch):
    """#421: 평가가 실패하면 쓰레기 버전이 registry에 쌓이지 않아야 한다."""
    registered = []
    monkeypatch.setenv("GCS_REGISTRY_PATH", "gs://fake/registry.db")
    monkeypatch.setattr(cli.build_training_dataset, "main", MagicMock())
    monkeypatch.setattr(cli.train, "main", lambda **kw: _pipeline_outcome())

    def _fail_evaluate(**kwargs):
        raise ValueError("y_true contains only one label (0)")

    monkeypatch.setattr(cli.evaluate, "main", _fail_evaluate)
    monkeypatch.setattr(
        cli.train, "register_pending_model", lambda pending: registered.append(pending)
    )

    with pytest.raises(ValueError, match="only one label"):
        cli.run_pipeline(
            dataset_path=None,
            events_start_date="2026-07-01",
            events_end_date="2026-07-08",
            config_path=None,
            model_output=None,
            test_set_output=None,
            feature_columns_output=None,
            categorical_columns_output=None,
            test_size=None,
            val_size=None,
            random_state=None,
            extra_features=None,
            experiment=None,
        )

    assert registered == []


def test_run_pipeline_fails_loudly_when_registration_fails(monkeypatch):
    """#421 리뷰(중간): 미룬 등록이 실패하면 run-pipeline이 실패해야 한다.

    삼키면 "파이프라인 완료" + exit 0으로 끝나 Airflow 태스크가 초록불이 되고,
    후속 promote-model은 어제 버전(=이미 champion)을 후보로 잡아 no-op이 된다.
    결과적으로 신규 후보가 없는 날이 어디에도 드러나지 않는다.
    """
    steps = []
    monkeypatch.setenv("GCS_REGISTRY_PATH", "gs://fake/registry.db")
    monkeypatch.setattr(cli.build_training_dataset, "main", MagicMock())
    monkeypatch.setattr(cli.train, "main", lambda **kw: _pipeline_outcome())
    monkeypatch.setattr(cli.evaluate, "main", lambda **kw: steps.append("evaluate"))

    def _fail_register(pending):
        steps.append("register")
        raise RuntimeError("registry 백엔드 없음(시뮬레이션)")

    monkeypatch.setattr(cli.train, "register_pending_model", _fail_register)

    with pytest.raises(RuntimeError, match="registry 백엔드 없음"):
        cli.run_pipeline(
            dataset_path=None,
            events_start_date="2026-07-01",
            events_end_date="2026-07-08",
            config_path=None,
            model_output=None,
            test_set_output=None,
            feature_columns_output=None,
            categorical_columns_output=None,
            test_size=None,
            val_size=None,
            random_state=None,
            extra_features=None,
            experiment=None,
        )

    # 평가는 통과한 뒤 등록에서 실패한 경로임을 고정한다.
    assert steps == ["evaluate", "register"]


def test_promote_model_prints_ok_and_exits_zero_on_success(monkeypatch, capsys):
    monkeypatch.setattr(
        cli.promote,
        "main",
        lambda **kwargs: _promotion_result(
            PromotionOutcome.PROMOTED,
            PromotionReasonCode.METRIC_NOT_DEGRADED,
        ),
    )

    cli.promote_model(
        model_name="ctr-model",
        champion_alias="champion",
        result_contract=None,
        result_path=None,
    )

    out = capsys.readouterr().out
    assert "[OK]" in out
    assert "v4" in out


def test_promote_model_accepts_deprecated_calibration_flag_and_warns(monkeypatch, capsys):
    # #390: calibration_model_name은 무시되지만, Airflow DAG 하위호환을 위해 인자는 받아들여야
    # 하고 promote.main으로는 전달되지 않아야 한다. 기본값과 다른 값이면 stderr 경고를 남긴다.
    captured = {}

    def _fake_main(**kwargs):
        captured.update(kwargs)
        return _promotion_result(
            PromotionOutcome.PROMOTED,
            PromotionReasonCode.METRIC_NOT_DEGRADED,
        )

    monkeypatch.setattr(cli.promote, "main", _fake_main)

    cli.promote_model(
        model_name="ctr-model",
        champion_alias="champion",
        calibration_model_name="something-else",
        result_contract=None,
        result_path=None,
    )

    assert "calibration_model_name" not in captured
    streams = capsys.readouterr()
    assert "[OK]" in streams.out
    assert "deprecated" in streams.err.lower()


def test_promote_model_prints_noop_message_when_no_candidate(monkeypatch, capsys):
    monkeypatch.setattr(
        cli.promote,
        "main",
        lambda **kwargs: _promotion_result(
            PromotionOutcome.NO_CANDIDATE,
            PromotionReasonCode.ALREADY_CHAMPION,
        ),
    )

    cli.promote_model(
        model_name="ctr-model",
        champion_alias="champion",
        result_contract=None,
        result_path=None,
    )

    out = capsys.readouterr().out
    assert "no-op" in out


def test_promote_model_legacy_rejection_exits_nonzero(monkeypatch, capsys):
    monkeypatch.setattr(
        cli.promote,
        "main",
        lambda **kwargs: _promotion_result(
            PromotionOutcome.REJECTED,
            PromotionReasonCode.METRIC_BELOW_CHAMPION,
        ),
    )

    with pytest.raises(typer.Exit) as exc_info:
        cli.promote_model(
            model_name="ctr-model",
            champion_alias="champion",
            result_contract=None,
            result_path=None,
        )

    assert exc_info.value.exit_code == 1
    err = capsys.readouterr().err
    assert "[게이트 미달]" in err
    assert "후보 ctr-model v4 val_roc_auc=0.8000" in err
    assert "champion(champion) val_roc_auc=0.7500" in err


def test_promote_model_preserves_legacy_calibration_rejection_detail(
    monkeypatch, capsys
) -> None:
    result = _promotion_result(
        PromotionOutcome.REJECTED,
        PromotionReasonCode.CALIBRATION_ARTIFACT_MISSING,
    ).with_legacy_message(
        "게이트2 미달: 후보 ctr-model v4는 "
        "downsampling(sampling_rate=0.5)인데 run(run-v4)에 "
        "calibration 아티팩트(calibration/calibration.json)가 없습니다."
    )
    monkeypatch.setattr(cli.promote, "main", lambda **kwargs: result)

    with pytest.raises(typer.Exit):
        cli.promote_model(
            model_name="ctr-model",
            champion_alias="champion",
            result_contract=None,
            result_path=None,
        )

    err = capsys.readouterr().err
    assert "sampling_rate=0.5" in err
    assert "run(run-v4)" in err
    assert "calibration/calibration.json" in err
    assert "legacy_message" not in result.model_dump_json()


def test_promote_model_exits_nonzero_with_error_prefix_on_unexpected_exception(
    monkeypatch, capsys
):
    def _raise(**kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(cli.promote, "main", _raise)

    with pytest.raises(typer.Exit) as exc_info:
        cli.promote_model(
            model_name="ctr-model",
            champion_alias="champion",
            result_contract=None,
            result_path=None,
        )

    assert exc_info.value.exit_code == 1
    err = capsys.readouterr().err
    assert "[에러]" in err


def test_promote_model_structured_rejection_writes_json_and_exits_zero(
    monkeypatch,
    capsys,
    tmp_path,
) -> None:
    result_path = tmp_path / "xcom" / "return.json"
    monkeypatch.setattr(
        cli.promote,
        "main",
        lambda **kwargs: _promotion_result(
            PromotionOutcome.REJECTED,
            PromotionReasonCode.METRIC_BELOW_CHAMPION,
        ),
    )

    cli.promote_model(
        model_name="ctr-model",
        champion_alias="champion",
        result_contract="model-promotion-result-v1",
        result_path=result_path,
    )

    stdout_result = json.loads(capsys.readouterr().out.strip())
    file_result = json.loads(result_path.read_text(encoding="utf-8"))
    assert stdout_result == file_result
    assert file_result["outcome"] == "rejected"


@pytest.mark.parametrize(
    ("result_contract", "result_path"),
    [
        ("model-promotion-result-v1", None),
        (None, Path("/airflow/xcom/return.json")),
        ("unknown-contract", Path("/airflow/xcom/return.json")),
    ],
)
def test_promote_model_rejects_invalid_structured_option_combinations_before_run(
    monkeypatch,
    result_contract,
    result_path,
) -> None:
    main = MagicMock()
    monkeypatch.setattr(cli.promote, "main", main)

    with pytest.raises(typer.Exit) as exc_info:
        cli.promote_model(
            model_name="ctr-model",
            champion_alias="champion",
            result_contract=result_contract,
            result_path=result_path,
        )

    assert exc_info.value.exit_code == 2
    main.assert_not_called()


def test_promote_model_structured_error_writes_safe_json_and_exits_one(
    monkeypatch,
    capsys,
    tmp_path,
) -> None:
    result_path = tmp_path / "return.json"

    def _raise(**kwargs):
        raise PromotionExecutionError(
            PromotionReasonCode.REGISTRY_ACCESS_FAILED,
            "credential=synthetic-private-value",
        )

    monkeypatch.setattr(cli.promote, "main", _raise)

    with pytest.raises(typer.Exit) as exc_info:
        cli.promote_model(
            model_name="ctr-model",
            champion_alias="champion",
            result_contract="model-promotion-result-v1",
            result_path=result_path,
        )

    assert exc_info.value.exit_code == 1
    streams = capsys.readouterr()
    payload = json.loads(streams.out.strip())
    assert payload["outcome"] == "error"
    assert payload["reason_code"] == "registry_access_failed"
    assert "synthetic-private-value" not in streams.out
    assert "registry_access_failed" in streams.err
    assert "synthetic-private-value" not in streams.err
    assert "synthetic-private-value" not in result_path.read_text(encoding="utf-8")


def test_promote_model_structured_error_preserves_known_context(
    monkeypatch,
    capsys,
    tmp_path,
) -> None:
    def _raise(**kwargs):
        raise PromotionExecutionError(
            PromotionReasonCode.ALIAS_UPDATE_FAILED,
            "safe diagnostic",
            candidate_version="4",
            champion_version="3",
            candidate_metric=0.80,
            champion_metric=0.75,
        )

    monkeypatch.setattr(cli.promote, "main", _raise)

    with pytest.raises(typer.Exit):
        cli.promote_model(
            model_name="ctr-model",
            champion_alias="champion",
            result_contract="model-promotion-result-v1",
            result_path=tmp_path / "return.json",
        )

    streams = capsys.readouterr()
    payload = json.loads(streams.out)
    assert payload["candidate_version"] == "4"
    assert payload["champion_version"] == "3"
    assert payload["candidate_metric"] == 0.80
    assert payload["champion_metric"] == 0.75


def test_promote_model_structured_unexpected_error_emits_safe_stack(
    monkeypatch,
    capsys,
    tmp_path,
) -> None:
    def _raise(**kwargs):
        raise RuntimeError("password=synthetic-private-value")

    monkeypatch.setattr(cli.promote, "main", _raise)

    with pytest.raises(typer.Exit):
        cli.promote_model(
            model_name="ctr-model",
            champion_alias="champion",
            result_contract="model-promotion-result-v1",
            result_path=tmp_path / "return.json",
        )

    streams = capsys.readouterr()
    assert "unexpected_error" in streams.err
    assert "RuntimeError" in streams.err
    assert "tests/test_cli.py" in streams.err
    assert "in _raise" in streams.err
    assert "synthetic-private-value" not in streams.err


def test_promote_model_result_write_failure_emits_safe_stdout_and_exits_one(
    monkeypatch,
    capsys,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        cli.promote,
        "main",
        lambda **kwargs: _promotion_result(
            PromotionOutcome.PROMOTED,
            PromotionReasonCode.METRIC_NOT_DEGRADED,
        ),
    )

    def _fail_write(*_args, **_kwargs) -> None:
        raise OSError("credential=synthetic-write-secret")

    monkeypatch.setattr(cli, "write_result_file", _fail_write)

    with pytest.raises(typer.Exit) as exc_info:
        cli.promote_model(
            model_name="ctr-model",
            champion_alias="champion",
            result_contract="model-promotion-result-v1",
            result_path=tmp_path / "return.json",
        )

    assert exc_info.value.exit_code == 1
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["outcome"] == "error"
    assert payload["reason_code"] == "result_write_failed"


# --- 실험용 피처 오버라이드 (#405) ---


def test_parse_extra_features_splits_and_trims():
    assert cli._parse_extra_features("a, b ,c") == ["a", "b", "c"]


def test_parse_extra_features_returns_none_for_blank():
    # 미지정·빈 문자열이면 prod 경로(계약 그대로)를 유지해야 한다.
    assert cli._parse_extra_features(None) is None
    assert cli._parse_extra_features("") is None
    assert cli._parse_extra_features("  ,  ") is None


def test_run_pipeline_shares_extra_features_between_train_and_evaluate(monkeypatch):
    """학습과 평가가 같은 실험 피처 목록을 써야 계약 검증이 어긋나지 않는다(#405)."""
    seen = {}
    monkeypatch.setenv("GCS_REGISTRY_PATH", "gs://fake/registry.db")
    monkeypatch.setattr(cli.build_training_dataset, "main", MagicMock())

    def _fake_train(**kwargs):
        seen["train"] = kwargs.get("extra_features")
        return _pipeline_outcome()

    monkeypatch.setattr(cli.train, "main", _fake_train)
    monkeypatch.setattr(
        cli.evaluate, "main", lambda **kw: seen.__setitem__("evaluate", kw.get("extra_features"))
    )
    monkeypatch.setattr(cli.train, "register_pending_model", MagicMock())

    cli.run_pipeline(
        dataset_path=None,
        events_start_date="2026-07-01",
        events_end_date="2026-07-08",
        config_path=None,
        model_output=None,
        test_set_output=None,
        feature_columns_output=None,
        categorical_columns_output=None,
        test_size=None,
        val_size=None,
        random_state=None,
        extra_features="views_per_day",
        experiment=None,
    )

    assert seen["train"] == ["views_per_day"]
    assert seen["evaluate"] == ["views_per_day"]


# --- 다중 시드 반복 학습 (#407) ---


def test_sweep_seeds_trains_per_seed_and_writes_summary(tmp_path, monkeypatch):
    """시드마다 학습하고 요약을 남긴다. 아티팩트는 시드별로 분리돼 덮어써지지 않는다."""
    calls = []

    def _fake_train(**kwargs):
        calls.append((kwargs["random_state"], kwargs["model_output"]))
        return train_module.TrainingOutcome(
            sampling_rate=1.0,
            run_id=f"run-{kwargs['random_state']}",
            val_roc_auc=0.70 + 0.01 * len(calls),
        )

    monkeypatch.setattr(cli.train, "main", _fake_train)
    result_path = tmp_path / "sweep.json"

    cli.sweep_seeds(
        seeds="42,43,44",
        config_path=None,
        data_path=None,
        output_dir=str(tmp_path / "artifacts"),
        test_size=None,
        val_size=None,
        experiment=None,
        extra_features=None,
        result_path=str(result_path),
    )

    assert [seed for seed, _ in calls] == [42, 43, 44]
    # 시드별 모델 경로가 서로 달라야 마지막 시드가 앞 시드를 덮어쓰지 않는다.
    assert len({path for _, path in calls}) == 3

    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload["seeds"] == [42, 43, 44]
    assert payload["summary"]["n"] == 3
    assert payload["summary"]["std"] is not None


def test_sweep_seeds_rejects_duplicate_seeds(tmp_path, monkeypatch):
    monkeypatch.setattr(cli.train, "main", MagicMock())

    with pytest.raises(ValueError):
        cli.sweep_seeds(
            seeds="42,42",
            config_path=None,
            data_path=None,
            output_dir=str(tmp_path / "artifacts"),
            test_size=None,
            val_size=None,
            experiment=None,
            extra_features=None,
            result_path=None,
        )


def test_verify_comparison_help_exposes_required_options() -> None:
    result = CliRunner().invoke(cli.app, ["verify-comparison", "--help"])

    assert result.exit_code == 0
    assert "--baseline-run-id" in result.output
    assert "--challenger-run-id" in result.output
    assert "--output" in result.output


def test_verify_comparison_cli_maps_validation_error_without_secret(
    monkeypatch,
) -> None:
    secret = "synthetic-private-value"

    def _raise(*_args, **_kwargs):
        raise cli.training_comparison.ComparisonValidationError(
            f"credential={secret}"
        )

    monkeypatch.setattr(cli.training_comparison, "verify_training_comparison", _raise)
    result = CliRunner().invoke(
        cli.app,
        [
            "verify-comparison",
            "--baseline-run-id",
            "baseline",
            "--challenger-run-id",
            "challenger",
            "--output",
            "comparison.json",
        ],
    )

    assert result.exit_code == 1
    assert "synthetic-private-value" not in result.output
    assert "ComparisonValidationError" in result.output
