#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#   "fastapi>=0.115,<1",
#   "uvicorn>=0.30,<1",
# ]
# ///
from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Final

import uvicorn
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_HOST: Final = "127.0.0.1"
DEFAULT_PORT: Final = 8765
DEFAULT_MODEL_ID: Final = "mock-model-v1"


class RerankRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1)
    video_ids: list[str] = Field(min_length=1)


class RerankResponseItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_id: str = Field(min_length=1)
    ctr_score: float
    model_id: str = Field(min_length=1)


class RerankResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[RerankResponseItem]


@dataclass(frozen=True, slots=True)
class MockServerSettings:
    model_id: str
    response_count: int | None


def _score(video_id: str) -> float:
    return round(0.5 + (len(video_id) % 40) / 100, 6)


def create_app(settings: MockServerSettings) -> FastAPI:
    app = FastAPI(title="Mock Inference Server", version="mock")

    @app.get("/healthcheck")
    def healthcheck() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/rerank", response_model=RerankResponse)
    def rerank(request: RerankRequest) -> RerankResponse:
        response_ids = request.video_ids
        if settings.response_count is not None:
            response_ids = response_ids[: settings.response_count]

        return RerankResponse(
            items=[
                RerankResponseItem(
                    video_id=video_id,
                    ctr_score=_score(video_id),
                    model_id=settings.model_id,
                )
                for video_id in response_ids
            ]
        )

    @app.get("/metrics", response_class=PlainTextResponse)
    def metrics() -> str:
        return (
            "# HELP rerank_requests_total Mock rerank request count.\n"
            "# TYPE rerank_requests_total counter\n"
            "rerank_requests_total 1\n"
            "# HELP rerank_video_ids_count Mock candidate count.\n"
            "# TYPE rerank_video_ids_count gauge\n"
            "rerank_video_ids_count 2\n"
        )

    return app


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a deterministic HTTP mock for serving smoke checks."
    )
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", default=DEFAULT_PORT, type=int)
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID)
    parser.add_argument(
        "--response-count",
        type=int,
        help="Return only the first N requested video IDs to simulate top-K.",
    )
    arguments = parser.parse_args()

    if not arguments.host.strip():
        parser.error("--host must not be empty")
    if not 1 <= arguments.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if not arguments.model_id.strip():
        parser.error("--model-id must not be empty")
    if arguments.response_count is not None and arguments.response_count < 1:
        parser.error("--response-count must be at least 1")

    return arguments


def main() -> None:
    arguments = parse_arguments()
    settings = MockServerSettings(
        model_id=arguments.model_id,
        response_count=arguments.response_count,
    )
    uvicorn.run(
        create_app(settings),
        host=arguments.host,
        port=arguments.port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
