"""Executor candidate HTTP router의 분리 경계를 검증한다.

전체 파이프라인에서 executor가 원격 검증 candidate를 보고하는 내부 HTTP 경계가 일반
Experiment workbench router와 분리돼 있음을 검증한다. 인증 wiring과 service transaction은
각각 app 조립부와 service의 책임이다.
"""

from __future__ import annotations

import importlib


def test_executor_candidate_route_is_owned_by_dedicated_router_module() -> None:
    """executor 내부 endpoint는 일반 Experiment router에 섞이지 않는다."""
    executor_module = importlib.import_module(
        "agent_orchestration.app.experiments.executor_router"
    )
    experiment_module = importlib.import_module("agent_orchestration.app.experiments.router")

    assert executor_module.router.prefix == "/internal/executor/experiments"
    assert any(
        route.path == "/internal/executor/experiments/{experiment_id}/candidate"
        for route in executor_module.router.routes
    )
    assert not hasattr(experiment_module, "executor_router")
