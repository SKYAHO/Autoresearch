"""feature_repo/env.py — prod/dev 환경 셀렉터·online 삭제 스캔 파생 검증(#399).

feast 의존이 없는 순수 헬퍼라 기본 pytest 그룹에서 실행된다. mapping 을 직접
주입해 프로세스 os.environ 을 건드리지 않고 검증한다.
"""

from __future__ import annotations

import pytest

from feature_repo.env import (
    ENV_DEV,
    ENV_PROD,
    ensure_online_store_env,
    online_full_scan_for_deletion,
    resolve_environment,
)


def test_resolve_environment_defaults_to_prod_when_unset() -> None:
    assert resolve_environment({}) == ENV_PROD


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_resolve_environment_blank_is_prod(blank: str) -> None:
    assert resolve_environment({"AUTORESEARCH_ENV": blank}) == ENV_PROD


@pytest.mark.parametrize(
    ("value", "expected"),
    [("dev", ENV_DEV), ("DEV", ENV_DEV), ("Prod", ENV_PROD), (" dev ", ENV_DEV)],
)
def test_resolve_environment_normalizes(value: str, expected: str) -> None:
    assert resolve_environment({"AUTORESEARCH_ENV": value}) == expected


def test_resolve_environment_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="AUTORESEARCH_ENV"):
        resolve_environment({"AUTORESEARCH_ENV": "staging"})


def test_full_scan_defaults_to_true_for_prod() -> None:
    # prod 회귀 가드: 미설정이면 full_scan=true(고아 키 GC 유지).
    assert online_full_scan_for_deletion({}) is True


def test_full_scan_is_false_for_dev() -> None:
    # dev=false 여야 apply 가 Redis 에 접속하지 않는다.
    assert online_full_scan_for_deletion({"AUTORESEARCH_ENV": "dev"}) is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [("true", True), ("false", False), ("True", True), ("0", False), ("on", True)],
)
def test_explicit_full_scan_wins_over_environment(value: str, expected: bool) -> None:
    # 배포가 명시한 값이 최종 권한을 가진다(apply Job 주입 경로).
    env = {"AUTORESEARCH_ENV": "dev", "FEAST_ONLINE_FULL_SCAN_FOR_DELETION": value}
    assert online_full_scan_for_deletion(env) is expected


def test_explicit_full_scan_blank_falls_back_to_derivation() -> None:
    env = {"AUTORESEARCH_ENV": "dev", "FEAST_ONLINE_FULL_SCAN_FOR_DELETION": "   "}
    assert online_full_scan_for_deletion(env) is False


def test_explicit_full_scan_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="FEAST_ONLINE_FULL_SCAN_FOR_DELETION"):
        online_full_scan_for_deletion(
            {"FEAST_ONLINE_FULL_SCAN_FOR_DELETION": "maybe"}
        )


def test_ensure_online_store_env_sets_prod_default() -> None:
    env: dict[str, str] = {}
    result = ensure_online_store_env(env)
    assert result == "true"
    assert env["FEAST_ONLINE_FULL_SCAN_FOR_DELETION"] == "true"


def test_ensure_online_store_env_derives_dev_false() -> None:
    env = {"AUTORESEARCH_ENV": "dev"}
    assert ensure_online_store_env(env) == "false"
    assert env["FEAST_ONLINE_FULL_SCAN_FOR_DELETION"] == "false"


def test_ensure_online_store_env_does_not_override_existing() -> None:
    env = {"AUTORESEARCH_ENV": "dev", "FEAST_ONLINE_FULL_SCAN_FOR_DELETION": "true"}
    assert ensure_online_store_env(env) == "true"
    assert env["FEAST_ONLINE_FULL_SCAN_FOR_DELETION"] == "true"
