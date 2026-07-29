from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import typer

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src import cli  # noqa: E402
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
    monkeypatch.setattr(cli.train, "main", lambda **kw: None)
    monkeypatch.setattr(cli.evaluate, "main", MagicMock())

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
    )

    # C2로 feast-only: build-features에 output_path + 기간만 넘긴다(duckdb 인자 없음).
    assert build_features_call == {
        "output_path": "dataset.csv",
        "events_start_date": "2026-07-01",
        "events_end_date": "2026-07-08",
    }


def test_run_pipeline_logs_feast_lineage_as_train_extra_params(monkeypatch):
    from src.features.feast_retrieval import DEFAULT_SERVICE

    train_call = {}
    monkeypatch.setenv("GCS_REGISTRY_PATH", "gs://fake/registry.db")
    monkeypatch.setattr(cli.build_training_dataset, "main", MagicMock())
    monkeypatch.setattr(cli.train, "main", lambda **kw: train_call.update(kw))
    monkeypatch.setattr(cli.evaluate, "main", MagicMock())

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
    )

    # feast-only lineage: assembly_source=feast + FeatureService + registry + 기간.
    assert train_call["extra_params"] == {
        "assembly_source": "feast",
        "feature_service": DEFAULT_SERVICE,
        "events_start_date": "2026-07-01",
        "events_end_date": "2026-07-08",
        "feast_registry_path": "gs://fake/registry.db",
    }


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
    assert "synthetic-private-value" not in result_path.read_text(encoding="utf-8")


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
