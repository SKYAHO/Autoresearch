"""실험/운영 tracking·registry 네임스페이스 분리 검증 (#406).

#396에서 로컬 에이전트가 `MLFLOW_TRACKING_URI=file:./mlruns`로 우회해 실험을
돌렸더니, `register_model`이 config의 `registry.model_name`을 그대로 써서 로컬
스토어에 **prod와 이름이 같은** `ctr-model` v1/v2가 등록됐다. 이름이 같으면
승격 게이트가 보는 것과 구분되지 않는다.
"""

from __future__ import annotations

import os

import pytest

from src.tracking.namespace import (
    EXPERIMENT_TRACKING_URI_DEFAULT,
    derive_experiment_name,
    PROD_EXPERIMENT_NAME,
    TrackingNamespace,
    is_experiment_model_name,
    resolve_tracking_namespace,
)


PROD_MODEL = "ctr-model"


# --- 운영 경로 ---


def test_prod_uses_configured_uri_and_prod_names() -> None:
    ns = resolve_tracking_namespace(
        prod_model_name=PROD_MODEL,
        experiment=None,
        tracking_uri_env="http://mlflow.mlflow:5000",
    )

    assert ns == TrackingNamespace(
        tracking_uri="http://mlflow.mlflow:5000",
        experiment_name=PROD_EXPERIMENT_NAME,
        registry_model_name=PROD_MODEL,
        is_experiment=False,
    )


@pytest.mark.parametrize("env_value", [None, "", "   "])
def test_prod_without_tracking_uri_fails_fast(env_value) -> None:
    """미설정이면 조용히 localhost:5000으로 가서 연결 오류로 죽지 않는다(#406).

    원인이 "트래킹 URI가 없다"라는 사실이 메시지에 드러나야 한다.
    """
    with pytest.raises(ValueError) as excinfo:
        resolve_tracking_namespace(
            prod_model_name=PROD_MODEL, experiment=None, tracking_uri_env=env_value
        )

    message = str(excinfo.value)
    assert "MLFLOW_TRACKING_URI" in message
    # 실험이면 로컬 기본 경로가 있다는 안내까지 준다.
    assert "--experiment" in message


# --- 실험 경로 ---


def test_experiment_without_tracking_uri_falls_back_to_local_store() -> None:
    ns = resolve_tracking_namespace(
        prod_model_name=PROD_MODEL, experiment="views_per_day", tracking_uri_env=None
    )

    assert ns.tracking_uri == EXPERIMENT_TRACKING_URI_DEFAULT
    assert ns.is_experiment is True


def test_experiment_registry_name_is_separated_from_prod() -> None:
    ns = resolve_tracking_namespace(
        prod_model_name=PROD_MODEL, experiment="views_per_day", tracking_uri_env=None
    )

    # 승격 게이트가 보는 prod 이름과 절대 겹치면 안 된다.
    assert ns.registry_model_name != PROD_MODEL
    assert ns.registry_model_name.startswith(f"{PROD_MODEL}-exp-")
    assert "views-per-day" in ns.registry_model_name


def test_experiment_name_is_separated_from_prod_experiment() -> None:
    ns = resolve_tracking_namespace(
        prod_model_name=PROD_MODEL, experiment="views_per_day", tracking_uri_env=None
    )

    assert ns.experiment_name != PROD_EXPERIMENT_NAME


def test_experiment_honours_explicit_tracking_uri() -> None:
    ns = resolve_tracking_namespace(
        prod_model_name=PROD_MODEL,
        experiment="exp1",
        tracking_uri_env="http://mlflow.mlflow:5000",
    )

    # 실험을 공용 서버에 남기고 싶을 수 있다 — URI는 존중하되 이름은 계속 분리된다.
    assert ns.tracking_uri == "http://mlflow.mlflow:5000"
    assert ns.registry_model_name != PROD_MODEL


@pytest.mark.parametrize(
    ("raw", "expected_fragment"),
    [
        ("views_per_day", "views-per-day"),
        ("Views Per Day", "views-per-day"),
        ("exp/001", "exp-001"),
        ("  spaced  ", "spaced"),
    ],
)
def test_experiment_name_is_slugified(raw, expected_fragment) -> None:
    ns = resolve_tracking_namespace(
        prod_model_name=PROD_MODEL, experiment=raw, tracking_uri_env=None
    )

    assert ns.registry_model_name == f"{PROD_MODEL}-exp-{expected_fragment}"


@pytest.mark.parametrize("raw", ["", "   ", "///", "___"])
def test_experiment_name_must_produce_a_usable_slug(raw) -> None:
    with pytest.raises(ValueError):
        resolve_tracking_namespace(
            prod_model_name=PROD_MODEL, experiment=raw, tracking_uri_env=None
        )


# --- 승격 차단용 판별 ---


def test_is_experiment_model_name_detects_experiment_namespace() -> None:
    assert is_experiment_model_name("ctr-model-exp-views-per-day") is True
    assert is_experiment_model_name("ctr-model") is False
    # prod 이름을 접두사로 가진 다른 정식 모델은 실험이 아니다.
    assert is_experiment_model_name("ctr-model-v2") is False


# --- 실험 좌표 암시 (#406 리뷰 1) ---


def test_extra_features_alone_implies_experiment_namespace() -> None:
    """--experiment 없이 --extra-features만 줘도 실험 좌표를 쓴다.

    그러지 않으면 prod 계약에 없는 입력으로 학습한 산출물이 prod registry 이름
    아래 쌓여, 이 이슈가 없애려던 상황이 그대로 재현된다.
    """
    assert derive_experiment_name(None, ["views_per_day"]) == "views_per_day"
    assert derive_experiment_name(None, ["a", "b"]) == "a-b"


def test_explicit_experiment_name_wins_over_derived() -> None:
    assert derive_experiment_name("my_exp", ["views_per_day"]) == "my_exp"


def test_no_experiment_and_no_extra_features_stays_prod() -> None:
    assert derive_experiment_name(None, None) is None
    assert derive_experiment_name(None, []) is None


def test_prod_model_name_containing_experiment_infix_is_rejected() -> None:
    """운영 이름에 구분자가 들어가면 승격 게이트가 운영 승격을 조용히 막는다."""
    with pytest.raises(ValueError) as excinfo:
        resolve_tracking_namespace(
            prod_model_name="ctr-model-exp-oops",
            experiment=None,
            tracking_uri_env="http://mlflow:5000",
        )

    assert "실험 구분자" in str(excinfo.value)


def test_slug_error_message_states_ascii_constraint() -> None:
    with pytest.raises(ValueError) as excinfo:
        resolve_tracking_namespace(
            prod_model_name=PROD_MODEL, experiment="실험", tracking_uri_env=None
        )

    assert "ASCII" in str(excinfo.value)


# --- 실험 로컬 스토어 경로 (#444) ---


def test_experiment_local_store_is_absolute() -> None:
    """CWD 상대 경로면 실행 위치에 따라 실험 이력이 갈린다(#444).

    에이전트가 여러 라운드를 이어 돌릴 때 앞 라운드 이력을 못 찾는다.
    """
    ns = resolve_tracking_namespace(
        prod_model_name=PROD_MODEL, experiment="exp1", tracking_uri_env=None
    )

    assert ns.tracking_uri.startswith("file:")
    path = ns.tracking_uri.removeprefix("file:")
    assert os.path.isabs(path), f"절대 경로가 아님: {path}"
    assert not path.startswith("./")


def test_experiment_local_store_is_stable_across_cwd(tmp_path, monkeypatch) -> None:
    """어느 디렉토리에서 실행하든 같은 위치를 가리켜야 한다."""
    from_root = resolve_tracking_namespace(
        prod_model_name=PROD_MODEL, experiment="exp1", tracking_uri_env=None
    ).tracking_uri

    monkeypatch.chdir(tmp_path)
    from_elsewhere = resolve_tracking_namespace(
        prod_model_name=PROD_MODEL, experiment="exp1", tracking_uri_env=None
    ).tracking_uri

    assert from_root == from_elsewhere


def test_explicit_tracking_uri_is_not_rewritten() -> None:
    """명시한 URI는 건드리지 않는다 — 절대 경로화는 기본값에만 적용된다."""
    ns = resolve_tracking_namespace(
        prod_model_name=PROD_MODEL, experiment="exp1", tracking_uri_env="file:./custom"
    )

    assert ns.tracking_uri == "file:./custom"
