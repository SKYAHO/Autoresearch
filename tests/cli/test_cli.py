from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from click import unstyle
import pytest
import typer
from typer.testing import CliRunner

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from autoresearch import cli  # noqa: E402
from autoresearch.model_evaluation.experiment_evaluation import POLICY_SEEDS  # noqa: E402
from autoresearch.model_evaluation.paired_experiment import (  # noqa: E402
    PairedExperimentRequest,
)
from applications.experiment_platform.workbench.client import (  # noqa: E402
    ApiConfigurationError,
    ApiUnavailableError,
    ExperimentApiError,
)
from tests.paired_experiment_fixtures import (  # noqa: E402
    paired_request_payload as _paired_request_payload,
    paired_result as _paired_result,
)
from autoresearch.model_evaluation.promotion_evidence import (  # noqa: E402
    ExperimentPlanReceipt,
    GcsObjectReceipt,
    PromotionEvidenceValidationError,
)
from autoresearch.model_training import train as train_module  # noqa: E402
from autoresearch.model_registry.promotion_result import (  # noqa: E402
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


def _fake_coverage(usable=2, missing=1):
    """build-features가 돌려주는 실측 커버리지(#464) — lineage 기록에 쓰인다."""
    days = [f"2026-07-{d:02d}" for d in range(1, usable + missing + 1)]
    return cli.build_training_dataset.SpineCoverage(
        requested_days=tuple(days),
        usable_days=tuple(days[:usable]),
        sparse_days=(),
        missing_days=tuple(days[usable:]),
        zero_click_days=(),
        total_rows=100,
        total_clicks=5,
    )


def _fake_assembly_outcome(usable=2, missing=1):
    """build_training_dataset.main 목(mock)의 반환값(#530) — AssemblyOutcome로 감싼다.

    ``main``이 ``AssemblyOutcome``을 돌려주도록 바뀌었으므로(#530), run-pipeline이 읽는
    ``assembly.coverage``가 실제로 존재하는 값이 되도록 여기서 감싼다. 이 테스트들은
    게시(snapshot_uri)를 다루지 않으므로 기본값 ``None``이면 충분하다.
    """
    return cli.build_training_dataset.AssemblyOutcome(
        coverage=_fake_coverage(usable=usable, missing=missing)
    )


# train.main 목(mock)의 최소 반환값(#530) — dataset_uri 재사용 경로 테스트는 등록
# 여부를 다루지 않으므로 pending_registration 기본값(None)이면 충분하다. 등록
# 시퀀스를 검증하는 테스트는 이 상수 대신 `_pipeline_outcome()`을 쓴다.
_OUTCOME_STUB = train_module.TrainingOutcome(sampling_rate=1.0, run_id="run-stub")


def test_snapshot_root_falls_back_to_environment(monkeypatch) -> None:
    """--snapshot-root 미지정 시 TRAINING_SNAPSHOT_ROOT를 쓴다(#530)."""
    monkeypatch.setenv("TRAINING_SNAPSHOT_ROOT", "gs://snapshots/training")
    assert cli._snapshot_root_kwargs(None) == {
        "snapshot_root": "gs://snapshots/training"
    }


def test_snapshot_root_option_wins_over_environment(monkeypatch) -> None:
    """--snapshot-root를 지정하면 환경변수보다 우선한다(#530)."""
    monkeypatch.setenv("TRAINING_SNAPSHOT_ROOT", "gs://from-env/training")
    assert cli._snapshot_root_kwargs("gs://explicit/training") == {
        "snapshot_root": "gs://explicit/training"
    }


def test_snapshot_root_absent_yields_no_kwarg(monkeypatch) -> None:
    """미설정이면 키 자체를 만들지 않아 main()의 기본값을 덮지 않는다."""
    monkeypatch.delenv("TRAINING_SNAPSHOT_ROOT", raising=False)
    assert cli._snapshot_root_kwargs(None) == {}


def test_run_pipeline_forwards_dates_to_build_features(monkeypatch):
    build_features_call = {}
    # build-features 성공 뒤 lineage가 GCS_REGISTRY_PATH를 필수로 읽는다(#359 C2, 무조건 기록).
    monkeypatch.setenv("GCS_REGISTRY_PATH", "gs://fake/registry.db")
    monkeypatch.setattr(
        cli.build_training_dataset,
        "main",
        lambda **kw: (build_features_call.update(kw), _fake_assembly_outcome())[1],
    )
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
        min_coverage_days=None,
    )

    # C2로 feast-only: build-features에 output_path + 기간만 넘긴다(duckdb 인자 없음).
    # min_coverage_days 미지정(None)이면 키 자체를 넘기지 않아 모듈 기본값이 살아 있다(#464).
    assert build_features_call == {
        "output_path": "dataset.csv",
        "events_start_date": "2026-07-01",
        "events_end_date": "2026-07-08",
    }


def test_train_model_forwards_explicit_seed_triplet_and_promotion_evidence(monkeypatch):
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
        experiment_plan_receipt="plan-receipt.json",
        promotion_evidence_root="gs://evidence/promotion-evidence",
    )

    assert (
        train_call["split_seed"],
        train_call["model_seed"],
        train_call["sampler_seed"],
    ) == (11, 12, 13)
    assert "require_snapshot" not in train_call
    assert train_call["experiment_plan_receipt_path"] == "plan-receipt.json"
    assert train_call["promotion_evidence_root"] == "gs://evidence/promotion-evidence"


def test_run_pipeline_requires_verified_snapshot_and_forwards_seed_triplet_and_promotion_evidence(
    monkeypatch,
):
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
        experiment_plan_receipt="plan-receipt.json",
        promotion_evidence_root="gs://evidence/promotion-evidence",
    )

    assert train_call["require_snapshot"] is True
    assert (
        train_call["split_seed"],
        train_call["model_seed"],
        train_call["sampler_seed"],
    ) == (11, 12, 13)
    assert train_call["experiment_plan_receipt_path"] == "plan-receipt.json"
    assert train_call["promotion_evidence_root"] == "gs://evidence/promotion-evidence"


def test_run_pipeline_forwards_coverage_override(monkeypatch):
    # 백필처럼 의도적으로 좁은 구간을 쓸 때 0으로 우회할 수 있어야 한다(#464).
    # None과 0을 구분하지 못하면(falsy 판정 등) 우회구가 조용히 무시된다.
    build_features_call = {}
    train_call = {}
    monkeypatch.setenv("GCS_REGISTRY_PATH", "gs://fake/registry.db")
    monkeypatch.setattr(
        cli.build_training_dataset,
        "main",
        lambda **kw: (build_features_call.update(kw), _fake_assembly_outcome())[1],
    )
    monkeypatch.setattr(
        cli.train, "main", lambda **kw: train_call.update(kw) or _pipeline_outcome()
    )
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
        min_coverage_days=0,
    )

    assert build_features_call["min_coverage_days"] == 0
    # 우회한 사실이 lineage에 남아야 정상 실행과 구별된다(#464 리뷰).
    assert train_call["extra_params"]["spine_coverage_guard"] == "off"
    assert train_call["extra_params"]["spine_coverage_min_days_applied"] == "0"


def test_run_pipeline_logs_feast_lineage_as_train_extra_params(monkeypatch):
    from autoresearch.feature_engineering.feast_retrieval import DEFAULT_SERVICE

    train_call = {}
    monkeypatch.setenv("GCS_REGISTRY_PATH", "gs://fake/registry.db")
    monkeypatch.setattr(
        cli.build_training_dataset, "main", MagicMock(return_value=_fake_assembly_outcome())
    )
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

    # feast-only lineage: assembly_source=feast + FeatureService + registry + 기간에,
    # 실측 spine 커버리지(#464 리뷰)를 더한다 — 요청 구간만으로는 v12처럼
    # "요청 3일 / 실제 2일"인 run을 사후에 구별할 수 없다.
    assert train_call["extra_params"] == {
        "assembly_source": "feast",
        "feature_service": DEFAULT_SERVICE,
        "events_start_date": "2026-07-01",
        "events_end_date": "2026-07-08",
        "feast_registry_path": "gs://fake/registry.db",
        "spine_requested_days": "3",
        "spine_usable_days": "2",
        "spine_missing_days": "1",
        "spine_sparse_days": "0",
        "spine_missing_day_list": "2026-07-03",
        "spine_coverage_min_days_applied": str(
            cli.build_training_dataset.DEFAULT_MIN_COVERAGE_DAYS
        ),
        "spine_coverage_guard": "on",
    }


def test_run_pipeline_rejects_dataset_uri_with_events_window(monkeypatch) -> None:
    """스냅샷이 구간을 확정했는데 다른 구간을 받으면 무엇이 진짜인지 답할 수 없다."""
    with pytest.raises(typer.BadParameter, match="dataset-uri"):
        cli.run_pipeline(
            dataset_uri="gs://snapshots/training/by-hash/" + "a" * 64 + "/",
            dataset_path=None,
            events_start_date="2026-07-26",
            events_end_date="2026-08-01",
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


def test_run_pipeline_rejects_dataset_uri_with_feature_service(monkeypatch) -> None:
    """재사용 경로는 조립 분기를 건너뛰어 --feature-service가 전달될 곳이 없다.

    거부하지 않으면 오퍼레이터가 --feature-service를 지정해도 조용히 무시되고,
    MLflow에는 다운로드한 manifest의 feature_service가 대신 남는다(#530 PR 리뷰).
    """
    with pytest.raises(typer.BadParameter, match="feature-service"):
        cli.run_pipeline(
            dataset_uri="gs://snapshots/training/by-hash/" + "a" * 64 + "/",
            dataset_path=None,
            events_start_date=None,
            events_end_date=None,
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
            feature_service="ctr_training_exp_v2",
        )


def test_run_pipeline_rejects_dataset_uri_with_snapshot_root(monkeypatch) -> None:
    """재사용 경로는 조립 분기를 건너뛰어 --snapshot-root가 전달될 곳이 없다.

    거부하지 않으면 오퍼레이터가 재사용 스냅샷을 이 루트에도 게시하려는 것으로
    오인할 수 있지만, 실제로는 아무 게시도 일어나지 않는다(#530 PR 리뷰).
    """
    with pytest.raises(typer.BadParameter, match="snapshot-root"):
        cli.run_pipeline(
            dataset_uri="gs://snapshots/training/by-hash/" + "a" * 64 + "/",
            dataset_path=None,
            events_start_date=None,
            events_end_date=None,
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
            snapshot_root="gs://snapshots/training",
        )


def test_run_pipeline_skips_assembly_and_logs_snapshot_uri(monkeypatch) -> None:
    """--dataset-uri면 조립을 건너뛰고 lineage를 manifest에서 채운다."""
    assembled: list[dict] = []
    monkeypatch.setattr(
        cli.build_training_dataset,
        "main",
        lambda **kwargs: assembled.append(kwargs),
    )
    captured: dict = {}
    monkeypatch.setattr(
        cli.train, "main", lambda **kwargs: captured.update(kwargs) or _OUTCOME_STUB
    )
    monkeypatch.setattr(cli.evaluate, "main", lambda **kwargs: None)

    uri = "gs://snapshots/training/by-hash/" + "a" * 64 + "/"
    cli.run_pipeline(
        dataset_uri=uri,
        dataset_path=None,
        events_start_date=None,
        events_end_date=None,
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

    assert assembled == []
    assert captured["dataset_uri"] == uri
    # 재사용 경로도 조립 경로와 같은 커버리지 하한을 적용해야 한다 — 넘기지
    # 않으면 재사용 경로만 게이트가 꺼진 채(min_coverage_days 기본값 0으로) 돈다.
    assert captured["min_coverage_days"] == cli.build_training_dataset.DEFAULT_MIN_COVERAGE_DAYS
    assert captured["extra_params"] == {
        "assembly_source": "snapshot_reuse",
        "training_snapshot_uri": uri,
    }


def test_run_pipeline_records_snapshot_uri_when_published(monkeypatch) -> None:
    """게시된 실행은 training_snapshot_uri를 MLflow 파라미터에 남긴다."""
    monkeypatch.setenv("GCS_REGISTRY_PATH", "gs://fake/registry.db")
    monkeypatch.setattr(
        cli.build_training_dataset,
        "main",
        lambda **kwargs: cli.build_training_dataset.AssemblyOutcome(
            coverage=_fake_coverage(), snapshot_uri="gs://snapshots/by-hash/abc/"
        ),
    )
    captured: dict = {}
    monkeypatch.setattr(
        cli.train, "main", lambda **kwargs: captured.update(kwargs) or _OUTCOME_STUB
    )
    monkeypatch.setattr(cli.evaluate, "main", lambda **kwargs: None)

    cli.run_pipeline(
        dataset_path=None,
        events_start_date="2026-07-26",
        events_end_date="2026-08-01",
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

    assert captured["extra_params"]["training_snapshot_uri"] == (
        "gs://snapshots/by-hash/abc/"
    )


def test_run_pipeline_omits_snapshot_uri_when_not_published(monkeypatch) -> None:
    """미게시 실행은 파라미터를 빈 문자열로 넣지 않고 아예 생략한다."""
    monkeypatch.setenv("GCS_REGISTRY_PATH", "gs://fake/registry.db")
    monkeypatch.setattr(
        cli.build_training_dataset,
        "main",
        lambda **kwargs: cli.build_training_dataset.AssemblyOutcome(
            coverage=_fake_coverage(), snapshot_uri=None
        ),
    )
    captured: dict = {}
    monkeypatch.setattr(
        cli.train, "main", lambda **kwargs: captured.update(kwargs) or _OUTCOME_STUB
    )
    monkeypatch.setattr(cli.evaluate, "main", lambda **kwargs: None)

    cli.run_pipeline(
        dataset_path=None,
        events_start_date="2026-07-26",
        events_end_date="2026-08-01",
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

    assert "training_snapshot_uri" not in captured["extra_params"]


def test_train_model_forwards_dataset_uri(monkeypatch) -> None:
    """--dataset-uri는 train.main에 그대로 전달돼야 재사용 다운로드가 일어난다(#530)."""
    train_call: dict = {}
    monkeypatch.setattr(cli.train, "main", lambda **kwargs: train_call.update(kwargs))

    uri = "gs://snapshots/training/by-hash/" + "b" * 64 + "/"
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
        split_seed=None,
        model_seed=None,
        sampler_seed=None,
        extra_features=None,
        experiment=None,
        experiment_plan_receipt=None,
        promotion_evidence_root=None,
        dataset_uri=uri,
    )

    assert train_call["dataset_uri"] == uri


def test_train_model_rejects_dataset_uri_with_data_path(monkeypatch) -> None:
    """스냅샷과 로컬 경로가 동시에 주어지면 어느 쪽이 학습 입력인지 결정할 수 없다."""
    with pytest.raises(typer.BadParameter, match="data-path"):
        cli.train_model(
            config_path=None,
            data_path="training_dataset.csv",
            model_output=None,
            test_set_output=None,
            feature_columns_output=None,
            categorical_columns_output=None,
            test_size=None,
            val_size=None,
            random_state=None,
            split_seed=None,
            model_seed=None,
            sampler_seed=None,
            extra_features=None,
            experiment=None,
            experiment_plan_receipt=None,
            promotion_evidence_root=None,
            dataset_uri="gs://snapshots/training/by-hash/" + "c" * 64 + "/",
        )


def test_train_model_forwards_min_coverage_days_default(monkeypatch) -> None:
    """--dataset-uri 재사용 경로도 조립 경로와 같은 커버리지 하한을 적용해야 한다(#530).

    --min-coverage-days를 지정하지 않았는데 train.main의 기본값(0, 게이트 꺼짐)이
    그대로 전달되면, 같은 스냅샷을 train-model로 재사용할 때와 run-pipeline으로
    재사용할 때 하한이 다르게 적용된다.
    """
    train_call: dict = {}
    monkeypatch.setattr(cli.train, "main", lambda **kwargs: train_call.update(kwargs))

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
        split_seed=None,
        model_seed=None,
        sampler_seed=None,
        extra_features=None,
        experiment=None,
        experiment_plan_receipt=None,
        promotion_evidence_root=None,
        dataset_uri="gs://snapshots/training/by-hash/" + "d" * 64 + "/",
        min_coverage_days=None,
    )

    assert (
        train_call["min_coverage_days"]
        == cli.build_training_dataset.DEFAULT_MIN_COVERAGE_DAYS
    )


def test_train_model_forwards_min_coverage_days_explicit_zero(monkeypatch) -> None:
    """0(명시적 우회)은 미지정과 구분해 그대로 전달해야 한다(#464와 같은 패턴)."""
    train_call: dict = {}
    monkeypatch.setattr(cli.train, "main", lambda **kwargs: train_call.update(kwargs))

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
        split_seed=None,
        model_seed=None,
        sampler_seed=None,
        extra_features=None,
        experiment=None,
        experiment_plan_receipt=None,
        promotion_evidence_root=None,
        dataset_uri="gs://snapshots/training/by-hash/" + "e" * 64 + "/",
        min_coverage_days=0,
    )

    assert train_call["min_coverage_days"] == 0


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
    assert "tests/cli/test_cli.py" in streams.err
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


# --- 데이터 조립 피처 보존 (#454) ---


def test_build_features_forwards_feature_service_and_extra_features(monkeypatch):
    """조립 옵션이 build_training_dataset.main까지 도달해야 한다(#454).

    도달하지 않으면 FeatureService에 파생 피처를 추가해도 CSV에서 잘려 학습의
    --extra-features가 승격할 컬럼 자체가 없다.
    """
    seen = {}
    monkeypatch.setattr(
        cli.build_training_dataset, "main", lambda **kwargs: seen.update(kwargs)
    )

    cli.build_features(
        output_path="experiment.csv",
        events_start_date="2026-07-01",
        events_end_date="2026-07-08",
        min_coverage_days=None,
        feature_service="ctr_experiment_v2",
        extra_features="views_per_day, like_per_view",
    )

    assert seen == {
        "output_path": "experiment.csv",
        "events_start_date": "2026-07-01",
        "events_end_date": "2026-07-08",
        "feature_service": "ctr_experiment_v2",
        "extra_features": ["views_per_day", "like_per_view"],
    }


def test_build_features_omits_unspecified_assembly_options(monkeypatch):
    # 미지정이면 키 자체를 넘기지 않아 prod 조립 인자가 기존과 완전히 동일하다(#454).
    seen = {}
    monkeypatch.setattr(
        cli.build_training_dataset, "main", lambda **kwargs: seen.update(kwargs)
    )

    cli.build_features(
        output_path=None,
        events_start_date="2026-07-01",
        events_end_date="2026-07-08",
        min_coverage_days=None,
        feature_service=None,
        extra_features=None,
    )

    assert seen == {
        "output_path": None,
        "events_start_date": "2026-07-01",
        "events_end_date": "2026-07-08",
    }


def test_run_pipeline_forwards_assembly_features_and_logs_actual_service(monkeypatch):
    """run-pipeline은 실험 피처를 조립에도 넘기고, 실제 FeatureService를 lineage에 남긴다(#454)."""
    build_features_call = {}
    train_call = {}
    monkeypatch.setenv("GCS_REGISTRY_PATH", "gs://fake/registry.db")
    monkeypatch.setattr(
        cli.build_training_dataset,
        "main",
        lambda **kw: (build_features_call.update(kw), _fake_assembly_outcome())[1],
    )
    monkeypatch.setattr(
        cli.train, "main", lambda **kw: train_call.update(kw) or _pipeline_outcome()
    )
    monkeypatch.setattr(cli.evaluate, "main", MagicMock())
    monkeypatch.setattr(cli.train, "register_pending_model", MagicMock())

    cli.run_pipeline(
        dataset_path="experiment.csv",
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
        feature_service="ctr_experiment_v2",
        experiment=None,
    )

    assert build_features_call["extra_features"] == ["views_per_day"]
    assert build_features_call["feature_service"] == "ctr_experiment_v2"
    # 하드코딩된 기본값이 남으면 실험 조립이 prod 서비스로 조회된 것처럼 기록된다.
    assert train_call["extra_params"]["feature_service"] == "ctr_experiment_v2"


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
    result = CliRunner().invoke(
        cli.app,
        ["verify-comparison", "--help"],
        color=False,
    )
    help_output = unstyle(result.output)

    assert result.exit_code == 0
    assert "--baseline-run-id" in help_output
    assert "--challenger-run-id" in help_output
    assert "--output" in help_output


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


class _PlanPublisher:
    """create-experiment-plan CLI의 publish 결과를 고정하는 test double."""

    def __init__(self) -> None:
        self.plan = None

    def publish_plan(self, plan):
        self.plan = plan
        return ExperimentPlanReceipt(
            plan=plan,
            object=GcsObjectReceipt(
                uri=f"gs://evidence/promotion-evidence/plans/{plan.plan_id}.json",
                generation="1",
                metageneration="1",
                time_created=datetime(2026, 8, 1, tzinfo=timezone.utc),
                sha256="a" * 64,
            ),
        )


def test_create_experiment_plan_cli_publishes_receipt_atomically(
    monkeypatch, tmp_path
) -> None:
    store = _PlanPublisher()
    roots: list[str] = []

    def _store(root: str) -> _PlanPublisher:
        roots.append(root)
        return store

    monkeypatch.setattr(cli, "PromotionEvidenceStore", _store, raising=False)
    output = tmp_path / "plan-receipt.json"
    result = CliRunner().invoke(
        cli.app,
        [
            "create-experiment-plan",
            "--hypothesis-id",
            "issue-466-h1",
            "--control-id",
            "control-revision",
            "--candidate-id",
            "candidate-revision",
            "--promotion-evidence-root",
            "gs://evidence/promotion-evidence",
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0
    assert roots == ["gs://evidence/promotion-evidence"]
    assert store.plan is not None
    assert ExperimentPlanReceipt.model_validate_json(output.read_text(encoding="utf-8")).plan == store.plan


@pytest.mark.parametrize("command", ["train-model", "run-pipeline"])
@pytest.mark.parametrize(
    "partial_option",
    [
        ("--experiment-plan-receipt", "plan-receipt.json"),
        ("--promotion-evidence-root", "gs://evidence/promotion-evidence"),
    ],
)
def test_training_cli_rejects_partial_promotion_evidence_options(
    command: str, partial_option: tuple[str, str]
) -> None:
    result = CliRunner().invoke(
        cli.app,
        [
            command,
            *partial_option,
        ],
    )

    assert result.exit_code == 2
    assert "--experiment-plan-receipt와 --promotion-evidence-root는 함께 지정해야 합니다" in result.output


def test_verify_comparison_cli_forwards_evidence_store_when_root_is_present(
    monkeypatch, tmp_path
) -> None:
    store = object()
    invocation = {}
    monkeypatch.setattr(
        cli,
        "PromotionEvidenceStore",
        lambda root: invocation.setdefault("root", root) and store,
        raising=False,
    )
    result_model = MagicMock()
    result_model.model_dump_json.return_value = "{}"
    monkeypatch.setattr(
        cli.training_comparison,
        "verify_training_comparison",
        lambda **kwargs: invocation.update(kwargs) or result_model,
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "verify-comparison",
            "--baseline-run-id",
            "baseline",
            "--challenger-run-id",
            "challenger",
            "--output",
            str(tmp_path / "comparison.json"),
            "--promotion-evidence-root",
            "gs://evidence/promotion-evidence",
        ],
    )

    assert result.exit_code == 0
    assert invocation["root"] == "gs://evidence/promotion-evidence"
    assert invocation["promotion_evidence_store"] is store


def test_verify_comparison_cli_omits_evidence_store_for_legacy_runs(
    monkeypatch, tmp_path
) -> None:
    invocation = {}
    result_model = MagicMock()
    result_model.model_dump_json.return_value = "{}"
    monkeypatch.setattr(
        cli.training_comparison,
        "verify_training_comparison",
        lambda **kwargs: invocation.update(kwargs) or result_model,
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "verify-comparison",
            "--baseline-run-id",
            "baseline",
            "--challenger-run-id",
            "challenger",
            "--output",
            str(tmp_path / "comparison.json"),
        ],
    )

    assert result.exit_code == 0
    assert "promotion_evidence_store" not in invocation


def test_create_experiment_plan_cli_hides_backend_error_and_does_not_write_output(
    monkeypatch, tmp_path
) -> None:
    secret = "synthetic-publisher-secret"

    class _FailingPublisher:
        def publish_plan(self, plan):
            raise PromotionEvidenceValidationError(f"credential={secret}")

    monkeypatch.setattr(
        cli,
        "PromotionEvidenceStore",
        lambda root: _FailingPublisher(),
        raising=False,
    )
    output = tmp_path / "plan-receipt.json"
    result = CliRunner().invoke(
        cli.app,
        [
            "create-experiment-plan",
            "--hypothesis-id",
            "issue-466-h1",
            "--control-id",
            "control-revision",
            "--candidate-id",
            "candidate-revision",
            "--promotion-evidence-root",
            "gs://evidence/promotion-evidence",
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 1
    assert not output.exists()
    assert secret not in result.output
    assert "PromotionEvidenceValidationError" in result.output


def test_create_experiment_plan_cli_preserves_published_receipt_when_local_write_fails(
    monkeypatch, tmp_path
) -> None:
    store = _PlanPublisher()
    monkeypatch.setattr(cli, "PromotionEvidenceStore", lambda root: store)

    def _raise_local_write(*_args, **_kwargs) -> None:
        raise OSError("synthetic local output failure")

    monkeypatch.setattr(cli, "write_manifest_atomic", _raise_local_write)
    output = tmp_path / "plan-receipt.json"
    result = CliRunner().invoke(
        cli.app,
        [
            "create-experiment-plan",
            "--hypothesis-id",
            "issue-466-h1",
            "--control-id",
            "control-revision",
            "--candidate-id",
            "candidate-revision",
            "--promotion-evidence-root",
            "gs://evidence/promotion-evidence",
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 1
    assert not output.exists()
    assert store.plan is not None
    assert "[실험 계획 receipt 저장 실패] OSError" in result.output
    assert "GCS plan은 이미 publish되었습니다" in result.output
    assert store.plan.plan_id in result.output


def _write_paired_request(tmp_path: Path, seeds: tuple[int, ...]) -> Path:
    request_path = tmp_path / "request.json"
    request_path.write_text(
        json.dumps(_paired_request_payload(seeds)), encoding="utf-8"
    )
    return request_path


def test_compare_paired_experiment_writes_passed_result_and_exits_zero(
    monkeypatch, tmp_path
) -> None:
    request_path = _write_paired_request(tmp_path, POLICY_SEEDS)
    output = tmp_path / "nested" / "result.json"
    monkeypatch.setattr(cli, "PromotionEvidenceStore", lambda root: object())

    def _evaluate(request, *, promotion_evidence_store, workspace, **kwargs):
        assert Path(workspace).is_dir()
        return _paired_result(request, outcome="comparison_passed")

    monkeypatch.setattr(cli.paired_experiment, "evaluate_paired_experiment", _evaluate)
    result = CliRunner().invoke(
        cli.app,
        [
            "compare-paired-experiment",
            "--request",
            str(request_path),
            "--promotion-evidence-root",
            "gs://evidence/promotion-evidence",
            "--output",
            str(output),
        ],
    )

    assert result.exit_code == 0
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["outcome"] == "comparison_passed"
    assert written["contract_version"] == "paired-offline-experiment-result-v1"


def test_compare_paired_experiment_failed_outcome_exits_one_with_result_file(
    monkeypatch, tmp_path
) -> None:
    request_path = _write_paired_request(tmp_path, POLICY_SEEDS)
    output = tmp_path / "result.json"
    monkeypatch.setattr(cli, "PromotionEvidenceStore", lambda root: object())
    monkeypatch.setattr(
        cli.paired_experiment,
        "evaluate_paired_experiment",
        lambda request, **kwargs: _paired_result(request, outcome="comparison_failed"),
    )
    result = CliRunner().invoke(
        cli.app,
        [
            "compare-paired-experiment",
            "--request",
            str(request_path),
            "--promotion-evidence-root",
            "gs://evidence/promotion-evidence",
            "--output",
            str(output),
        ],
    )

    # 실패도 결과 파일을 남긴다 — 후속 게이트가 사유 없이 판정을 추정하지 않도록.
    assert result.exit_code == 1
    assert json.loads(output.read_text(encoding="utf-8"))["outcome"] == "comparison_failed"


def test_compare_paired_experiment_rejects_invalid_request_before_running(
    monkeypatch, tmp_path
) -> None:
    request_path = tmp_path / "request.json"
    payload = _paired_request_payload(POLICY_SEEDS)
    payload["candidate_sha"] = "not-a-sha"
    request_path.write_text(json.dumps(payload), encoding="utf-8")
    called: list[object] = []
    monkeypatch.setattr(cli, "PromotionEvidenceStore", lambda root: object())
    monkeypatch.setattr(
        cli.paired_experiment,
        "evaluate_paired_experiment",
        lambda *args, **kwargs: called.append(args),
    )
    result = CliRunner().invoke(
        cli.app,
        [
            "compare-paired-experiment",
            "--request",
            str(request_path),
            "--promotion-evidence-root",
            "gs://evidence/promotion-evidence",
            "--output",
            str(tmp_path / "result.json"),
        ],
    )

    assert result.exit_code == 2
    assert called == []
    assert "ValidationError" in result.output
    assert not (tmp_path / "result.json").exists()


def test_compare_paired_experiment_help_exposes_required_options() -> None:
    result = CliRunner().invoke(
        cli.app, ["compare-paired-experiment", "--help"], color=False
    )
    help_output = unstyle(result.output)

    assert result.exit_code == 0
    assert "--request" in help_output
    assert "--promotion-evidence-root" in help_output
    assert "--output" in help_output


def _degradation_result(**overrides):
    from autoresearch.model_evaluation.degradation_eval import (
        DegradationPoint,
        RollingOriginResult,
    )
    from autoresearch.model_training.training_provenance import (
        DatasetColumn,
        TrainingSnapshotManifest,
    )

    manifest = TrainingSnapshotManifest(
        dataset_sha256="0" * 64,
        schema_sha256="1" * 64,
        row_count=10,
        columns=[DatasetColumn(name="clicked", dtype="int64")],
        created_at="2026-07-20T00:00:00Z",
        events_start_date="2026-07-17",
        events_end_date="2026-07-19",
        feature_service="ctr_training_v1",
        registry_uri="gs://fake/registry.db",
        registry_generation="1",
        registry_sha256="2" * 64,
    )
    defaults = dict(
        cutoff_date="2026-07-20",
        window_days=3,
        horizon_days=1,
        baseline_val_roc_auc=0.80,
        min_auc_drop=0.05,
        per_day=[],
        degradation_point=DegradationPoint(reason="insufficient_valid_points"),
        training_snapshot_manifest=manifest,
    )
    defaults.update(overrides)
    return RollingOriginResult(**defaults)


def test_measure_degradation_writes_result_and_exits_zero(monkeypatch, tmp_path):
    output = tmp_path / "nested" / "result.json"
    captured = {}

    def _fake_run_rolling_origin(cutoff_date, **kwargs):
        captured["cutoff_date"] = cutoff_date
        captured.update(kwargs)
        return _degradation_result()

    monkeypatch.setattr(cli.degradation_eval, "run_rolling_origin", _fake_run_rolling_origin)

    result = CliRunner().invoke(
        cli.app,
        [
            "measure-degradation",
            "--cutoff-date", "2026-07-20",
            "--window-days", "3",
            "--horizon-days", "1",
            "--run-root", str(tmp_path / "run"),
            "--min-rows-per-day", "3",
            "--min-auc-drop", "0.05",
            "--output", str(output),
        ],
    )

    assert result.exit_code == 0
    assert captured["cutoff_date"] == "2026-07-20"
    assert captured["window_days"] == 3
    assert captured["horizon_days"] == 1
    assert captured["min_rows_per_day"] == 3
    assert captured["min_auc_drop"] == 0.05
    assert captured["best_effort"] is False
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["cutoff_date"] == "2026-07-20"


def test_measure_degradation_forwards_best_effort_flag(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(
        cli.degradation_eval,
        "run_rolling_origin",
        lambda cutoff_date, **kwargs: (captured.update(kwargs), _degradation_result())[1],
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "measure-degradation",
            "--cutoff-date", "2026-07-20",
            "--window-days", "3",
            "--horizon-days", "1",
            "--run-root", str(tmp_path / "run"),
            "--min-rows-per-day", "3",
            "--min-auc-drop", "0.05",
            "--output", str(tmp_path / "result.json"),
            "--best-effort",
        ],
    )

    assert result.exit_code == 0
    assert captured["best_effort"] is True


def test_measure_degradation_run_root_exists_error_exits_two(monkeypatch, tmp_path):
    from autoresearch.model_evaluation.degradation_eval import RunRootExistsError

    def _raise(cutoff_date, **kwargs):
        raise RunRootExistsError("run_root가 이미 존재합니다")

    monkeypatch.setattr(cli.degradation_eval, "run_rolling_origin", _raise)
    output = tmp_path / "result.json"

    result = CliRunner().invoke(
        cli.app,
        [
            "measure-degradation",
            "--cutoff-date", "2026-07-20",
            "--window-days", "3",
            "--horizon-days", "1",
            "--run-root", str(tmp_path / "run"),
            "--min-rows-per-day", "3",
            "--min-auc-drop", "0.05",
            "--output", str(output),
        ],
    )

    assert result.exit_code == 2
    assert not output.exists()


def test_measure_degradation_other_failure_exits_one(monkeypatch, tmp_path):
    def _raise(cutoff_date, **kwargs):
        raise RuntimeError("BigQuery 조립 실패")

    monkeypatch.setattr(cli.degradation_eval, "run_rolling_origin", _raise)
    output = tmp_path / "result.json"

    result = CliRunner().invoke(
        cli.app,
        [
            "measure-degradation",
            "--cutoff-date", "2026-07-20",
            "--window-days", "3",
            "--horizon-days", "1",
            "--run-root", str(tmp_path / "run"),
            "--min-rows-per-day", "3",
            "--min-auc-drop", "0.05",
            "--output", str(output),
        ],
    )

    assert result.exit_code == 1
    assert not output.exists()
    assert "BigQuery" not in result.output  # 원문 예외 메시지를 그대로 노출하지 않는다


def test_measure_degradation_help_exposes_required_options() -> None:
    result = CliRunner().invoke(cli.app, ["measure-degradation", "--help"], color=False)
    help_output = unstyle(result.output)

    assert result.exit_code == 0
    assert "--cutoff-date" in help_output
    assert "--window-days" in help_output
    assert "--horizon-days" in help_output
    assert "--min-auc-drop" in help_output
    assert "--best-effort" in help_output


class _StubExperimentClient:
    """CLI 배선만 보는 client double — HTTP 형태는 ui client 테스트가 본다."""

    def __init__(self, status: str, *, fail_on: str | None = None) -> None:
        self.status = status
        self.fail_on = fail_on
        self.calls: list[tuple[str, str, object]] = []

    def get_experiment(self, experiment_id: str):
        return SimpleNamespace(status=self.status)

    def patch_status(self, experiment_id, status, *, reason=None, metric_snapshot=None):
        self.calls.append((status, reason or "", metric_snapshot))
        if status == self.fail_on:
            raise ApiUnavailableError("전이에 실패했습니다.")
        self.status = status
        return SimpleNamespace(status=status)

    def post_log(self, experiment_id, *, idempotency_key, content, log_type="stdout"):
        self.calls.append(("LOG", idempotency_key, None))
        return None

    @property
    def patched(self) -> list[str]:
        return [call[0] for call in self.calls if call[0] != "LOG"]


def _install_stub_client(monkeypatch, stub) -> None:
    """지연 import되는 client 모듈 전체를 double로 바꾼다."""
    monkeypatch.setattr(
        cli,
        "_experiment_client_module",
        lambda: SimpleNamespace(
            ExperimentClient=SimpleNamespace(from_environment=lambda: stub),
            ExperimentApiError=ExperimentApiError,
        ),
    )


def _write_paired_result(tmp_path: Path, outcome: str) -> Path:
    request = PairedExperimentRequest.model_validate(
        _paired_request_payload((42, 43, 44))
    )
    path = tmp_path / "result.json"
    path.write_text(
        _paired_result(request, outcome=outcome).model_dump_json(), encoding="utf-8"
    )
    return path


_EXPERIMENT_UUID = "11111111-1111-1111-1111-111111111111"


def _run_report(result_path: Path):
    return CliRunner().invoke(
        cli.app,
        [
            "report-experiment-result",
            "--result",
            str(result_path),
            "--experiment-id",
            _EXPERIMENT_UUID,
        ],
    )


def test_report_result_walks_remaining_transitions(tmp_path, monkeypatch) -> None:
    """RUNNING에서 시작하면 EVALUATING→PASSED를 밟고 지표는 마지막에만 싣는다."""
    stub = _StubExperimentClient("RUNNING")
    _install_stub_client(monkeypatch, stub)

    outcome = _run_report(_write_paired_result(tmp_path, "comparison_passed"))

    assert outcome.exit_code == 0
    assert stub.patched == ["EVALUATING", "PASSED"]
    snapshots = [call[2] for call in stub.calls if call[0] != "LOG"]
    assert snapshots[0] is None
    assert snapshots[1] is not None


def test_report_result_resumes_from_evaluating(tmp_path, monkeypatch) -> None:
    """이미 EVALUATING이면 터미널 전이 하나만 남는다."""
    stub = _StubExperimentClient("EVALUATING")
    _install_stub_client(monkeypatch, stub)

    outcome = _run_report(_write_paired_result(tmp_path, "comparison_rejected"))

    assert outcome.exit_code == 0
    assert stub.patched == ["FAILED"]


def test_report_result_refuses_created_experiment(tmp_path, monkeypatch) -> None:
    """CREATED는 launcher가 선점할 행이므로 건드리지 않고 거부한다(#547).

    자가 claim은 #547 병합 전까지만 안전했다. 지금 RUNNING으로 올리면 launcher의
    `CREATED_CLAIM_STATEMENT`와 같은 행을 두고 경합한다.
    """
    stub = _StubExperimentClient("CREATED")
    _install_stub_client(monkeypatch, stub)

    outcome = _run_report(_write_paired_result(tmp_path, "comparison_passed"))

    assert outcome.exit_code == 1
    assert stub.calls == []
    assert stub.status == "CREATED"
    # 운영 대응이 다른 실패이므로 진단이 구분돼야 한다 — 이쪽은 기다리면 풀린다.
    output = unstyle(outcome.output)
    assert "LauncherOwnedExperimentError" in output
    assert "선점된 뒤 다시 실행" in output


def test_report_result_leaves_intermediate_state_for_resume(tmp_path, monkeypatch) -> None:
    """중간 실패는 실험을 그 상태 그대로 둔다 — ERROR로 내리지 않는다.

    ERROR는 터미널이라 강등하면 재실행이 막히고, 지표와 포인터 로그는 그 이후 단계라
    영영 기록되지 않는다. launcher는 `executor_job_created_at`이 찍힌 행을 두 claim
    쿼리 어디에서도 보지 않으므로(`launcher/repository.py`), 중간 상태로 남겨도
    경합하지 않는다.
    """
    stub = _StubExperimentClient("RUNNING", fail_on="PASSED")
    _install_stub_client(monkeypatch, stub)

    outcome = _run_report(_write_paired_result(tmp_path, "comparison_passed"))

    assert outcome.exit_code == 1
    assert stub.patched == ["EVALUATING", "PASSED"]
    assert stub.status == "EVALUATING"
    assert "EVALUATING 상태로 남았습니다" in unstyle(outcome.output)


def test_report_result_resumes_after_transient_failure(tmp_path, monkeypatch) -> None:
    """일시적 실패 뒤 재실행이 남은 전이를 마치고 지표·로그까지 기록한다.

    강등을 두면 이 재개가 불가능하다 — 이 테스트가 그 결정의 값을 고정한다.
    """
    stub = _StubExperimentClient("RUNNING", fail_on="PASSED")
    _install_stub_client(monkeypatch, stub)
    result_path = _write_paired_result(tmp_path, "comparison_passed")

    first = _run_report(result_path)
    stub.fail_on = None
    stub.calls.clear()
    second = _run_report(result_path)

    assert first.exit_code == 1
    assert second.exit_code == 0
    assert stub.patched == ["PASSED"]
    assert stub.status == "PASSED"
    # 지표와 포인터 로그가 결국 기록된다.
    assert [call[2] for call in stub.calls if call[0] == "PASSED"][0] is not None
    assert any(call[0] == "LOG" for call in stub.calls)


def test_report_result_refuses_terminal_without_touching_experiment(
    tmp_path, monkeypatch
) -> None:
    """이미 다른 터미널이면 전이를 하나도 밟지 않고 거부한다 — ERROR 강등도 없다.

    거부가 전이 계획 단계에서 일어나므로 호출자의 `reached`는 None으로 남고,
    `_demote_to_error`가 실험을 건드리지 않아야 한다.
    """
    stub = _StubExperimentClient("FAILED")
    _install_stub_client(monkeypatch, stub)

    outcome = _run_report(_write_paired_result(tmp_path, "comparison_passed"))

    assert outcome.exit_code == 1
    assert stub.calls == []
    assert stub.status == "FAILED"
    # CREATED 거부와 같은 종료 코드지만 대응이 다르다 — 기다려도 풀리지 않는다.
    output = unstyle(outcome.output)
    assert "TerminalStatusConflictError" in output
    assert "--experiment-id가 맞는지" in output


def test_report_result_rejects_invalid_result_json(tmp_path, monkeypatch) -> None:
    """계약 위반 JSON은 API를 부르기 전에 종료 코드 2로 거부한다."""
    stub = _StubExperimentClient("CREATED")
    _install_stub_client(monkeypatch, stub)
    path = tmp_path / "bad.json"
    path.write_text('{"outcome": "comparison_passed"}', encoding="utf-8")

    outcome = _run_report(path)

    assert outcome.exit_code == 2
    assert stub.calls == []
    assert "[결과 반영 실패] ValidationError" in unstyle(outcome.output)


def test_report_result_maps_configuration_error_to_exit_2(tmp_path, monkeypatch) -> None:
    """토큰 미설정은 client 생성자가 이미 막는다 — CLI는 그 예외를 종료 코드로 옮긴다."""

    def _raise() -> None:
        raise ApiConfigurationError("ORCH_UI_API_TOKEN을 설정해 주세요.")

    monkeypatch.setattr(
        cli,
        "_experiment_client_module",
        lambda: SimpleNamespace(
            ExperimentClient=SimpleNamespace(from_environment=_raise),
            ExperimentApiError=ExperimentApiError,
        ),
    )

    outcome = _run_report(_write_paired_result(tmp_path, "comparison_passed"))

    assert outcome.exit_code == 2
    assert "[결과 반영 실패] ApiConfigurationError" in unstyle(outcome.output)


def test_report_result_is_safe_to_rerun(tmp_path, monkeypatch) -> None:
    """같은 명령을 두 번 돌려도 전이가 중복되지 않고 로그 key가 동일하다.

    PATCH는 서버 멱등성이 없으므로(`service.py:286-288`) 재실행이 event를 늘리면
    안 된다. 두 번째 실행은 이미 목표 터미널이라 전이를 하나도 밟지 않는다.
    """
    stub = _StubExperimentClient("RUNNING")
    _install_stub_client(monkeypatch, stub)
    result_path = _write_paired_result(tmp_path, "comparison_passed")

    first = _run_report(result_path)
    first_calls = list(stub.calls)
    stub.calls.clear()
    second = _run_report(result_path)

    assert first.exit_code == 0
    assert second.exit_code == 0
    # 두 번째 실행은 상태를 전혀 건드리지 않는다.
    assert stub.patched == []
    # 로그는 다시 보내지만 key가 같아 서버가 중복을 막는다.
    first_log_key = [call[1] for call in first_calls if call[0] == "LOG"]
    second_log_key = [call[1] for call in stub.calls if call[0] == "LOG"]
    assert first_log_key == second_log_key


def test_cli_import_does_not_require_sqlalchemy() -> None:
    """`autoresearch.cli` import가 SQLAlchemy를 끌어오지 않아야 한다.

    학습 이미지는 `uv sync --locked --no-dev`로 빌드되어 SQLAlchemy가 없다.
    `applications.experiment_platform.workbench.client`를 top-level import하면 그 모듈이 `ui.models` →
    `app.experiments.models` → sqlalchemy로 이어져, 그 이미지에서 `train-model --help`
    조차 뜨지 않는다(CI `Docker build (train)` smoke check가 이걸 잡았다).

    별도 프로세스에서 확인한다 — 같은 프로세스는 다른 테스트가 이미 import해 둔다.
    """
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import autoresearch.cli, sys; "
            "assert 'sqlalchemy' not in sys.modules, "
            "'src.cli가 SQLAlchemy를 전이 의존으로 끌어온다'",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def test_report_result_rejects_non_uuid_experiment_id(tmp_path, monkeypatch) -> None:
    """UUID 오타는 인자 오류(2)다 — API 실패(1)와 섞이면 호출자가 재시도를 판단 못 한다.

    #454의 `experiment_id`는 UUID 형식이 아니므로, 두 좌표를 뒤바꿔 넣은 사고도
    네트워크를 쓰기 전에 여기서 걸린다.
    """
    stub = _StubExperimentClient("RUNNING")
    _install_stub_client(monkeypatch, stub)
    result_path = _write_paired_result(tmp_path, "comparison_passed")

    outcome = CliRunner().invoke(
        cli.app,
        [
            "report-experiment-result",
            "--result",
            str(result_path),
            "--experiment-id",
            "primary",
        ],
    )

    assert outcome.exit_code == 2
    assert stub.calls == []
    assert "UUID 형식이 아닙니다" in unstyle(outcome.output)
