#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#   "httpx>=0.28,<0.29",
#   "pydantic>=2,<3",
# ]
# ///
"""실행 중인 Inference Server의 HTTP serving smoke 검증.

이 스크립트는 FastAPI ``/healthcheck``·``/rerank``·``/metrics``를 실제 HTTP로
호출한다. TestClient, fake reader, Feast SDK 직접 조회를 사용하지 않으므로 Redis
online store와 모델이 연결된 서버가 먼저 실행되어 있어야 한다.

사용법::

    uv run scripts/verify_serving_e2e.py \
      --base-url http://127.0.0.1:8000 \
      --user-id user-0001 \
      --video-id video-0001 \
      --video-id video-0002

``--expected-count``를 지정하면 top K처럼 요청 후보 중 일부를 반환하는 계약도
검증할 수 있다. 지정하지 않으면 요청한 모든 후보가 반환되어야 한다.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, TypeVar

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

_DEFAULT_BASE_URL: Final = "http://127.0.0.1:8000"
_DEFAULT_TIMEOUT_SECONDS: Final = 30.0
_REQUIRED_METRICS: Final[tuple[str, ...]] = (
    "rerank_requests_total",
    "rerank_video_ids_count",
)


class SmokeCheckError(RuntimeError):
    pass


_ModelT = TypeVar("_ModelT", bound=BaseModel)


class HealthcheckResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str


class RerankResponseItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_id: str = Field(min_length=1)
    ctr_score: float
    model_id: str = Field(min_length=1)


class RerankResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[RerankResponseItem]


@dataclass(frozen=True, slots=True)
class SmokeArguments:
    base_url: str
    user_id: str
    video_ids: tuple[str, ...]
    expected_count: int
    expected_model_id: str | None
    timeout_seconds: float


def parse_arguments(argv: Sequence[str] | None = None) -> SmokeArguments:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=_DEFAULT_BASE_URL)
    parser.add_argument("--user-id", required=True)
    parser.add_argument(
        "--video-id",
        dest="video_ids",
        action="append",
        required=True,
        help="검증할 영상 ID. 여러 번 지정한다.",
    )
    parser.add_argument(
        "--expected-count",
        type=int,
        default=None,
        help="기대하는 응답 item 수. 기본값은 요청 영상 수.",
    )
    parser.add_argument("--expected-model-id", default=None)
    parser.add_argument(
        "--timeout",
        type=float,
        default=_DEFAULT_TIMEOUT_SECONDS,
        dest="timeout_seconds",
    )
    parsed = parser.parse_args(argv)

    video_ids = tuple(parsed.video_ids)
    expected_count = (
        parsed.expected_count if parsed.expected_count is not None else len(video_ids)
    )
    if not parsed.user_id:
        parser.error("--user-id must not be empty")
    if any(not video_id for video_id in video_ids):
        parser.error("--video-id must not contain empty values")
    if len(set(video_ids)) != len(video_ids):
        parser.error("--video-id values must be unique")
    if not 1 <= expected_count <= len(video_ids):
        parser.error("--expected-count must be between 1 and the requested video count")
    if parsed.timeout_seconds <= 0:
        parser.error("--timeout must be greater than zero")

    return SmokeArguments(
        base_url=_normalize_base_url(parsed.base_url),
        user_id=parsed.user_id,
        video_ids=video_ids,
        expected_count=expected_count,
        expected_model_id=parsed.expected_model_id,
        timeout_seconds=parsed.timeout_seconds,
    )


def _normalize_base_url(value: str) -> str:
    base_url = value.rstrip("/")
    parsed = httpx.URL(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.host:
        raise ValueError("--base-url must be an http(s) URL with a host")
    if parsed.username or parsed.password:
        raise ValueError("--base-url must not contain credentials")
    return base_url


def _request_json(
    client: httpx.Client,
    method: str,
    path: str,
    *,
    response_model: type[_ModelT],
    request_body: Mapping[str, str | list[str]] | None = None,
) -> _ModelT:
    try:
        response = client.request(method, path, json=request_body)
        response.raise_for_status()
        return response_model.model_validate(response.json())
    except httpx.HTTPStatusError as error:
        raise SmokeCheckError(
            f"{path} returned HTTP {error.response.status_code}."
        ) from None
    except httpx.HTTPError as error:
        raise SmokeCheckError(
            f"{path} request failed: {type(error).__name__}."
        ) from None
    except json.JSONDecodeError:
        raise SmokeCheckError(f"{path} returned invalid JSON.") from None
    except ValidationError as error:
        raise SmokeCheckError(f"{path} response does not match its contract.") from error


def _request_metrics(client: httpx.Client) -> str:
    try:
        response = client.get("/metrics")
        response.raise_for_status()
        return response.text
    except httpx.HTTPStatusError as error:
        raise SmokeCheckError(
            f"/metrics returned HTTP {error.response.status_code}."
        ) from None
    except httpx.HTTPError as error:
        raise SmokeCheckError(
            f"/metrics request failed: {type(error).__name__}."
        ) from None


def _validate_healthcheck(healthcheck: HealthcheckResponse) -> None:
    if healthcheck.status != "ok":
        raise SmokeCheckError(
            f"/healthcheck returned unexpected status: {healthcheck.status!r}."
        )


def _validate_rerank(
    rerank: RerankResponse,
    *,
    requested_video_ids: tuple[str, ...],
    expected_count: int,
    expected_model_id: str | None,
) -> RerankResponse:
    if len(rerank.items) != expected_count:
        raise SmokeCheckError(
            f"/rerank returned {len(rerank.items)} items; expected {expected_count}."
        )

    requested_ids = set(requested_video_ids)
    response_ids = tuple(item.video_id for item in rerank.items)
    if len(set(response_ids)) != len(response_ids):
        raise SmokeCheckError("/rerank response contains duplicate video IDs.")
    if not set(response_ids).issubset(requested_ids):
        raise SmokeCheckError("/rerank response contains an unrequested video ID.")
    if expected_count == len(requested_video_ids) and set(response_ids) != requested_ids:
        raise SmokeCheckError("/rerank response does not contain all requested video IDs.")

    model_ids = {item.model_id for item in rerank.items}
    if len(model_ids) != 1:
        raise SmokeCheckError("/rerank response contains inconsistent model IDs.")
    model_id = next(iter(model_ids))
    if expected_model_id is not None and model_id != expected_model_id:
        raise SmokeCheckError(
            f"/rerank returned model_id {model_id!r}; expected {expected_model_id!r}."
        )
    if any(not math.isfinite(item.ctr_score) or not 0 <= item.ctr_score <= 1 for item in rerank.items):
        raise SmokeCheckError("/rerank response contains an invalid CTR score.")
    return rerank


def _validate_metrics(metrics: str) -> None:
    missing = tuple(name for name in _REQUIRED_METRICS if name not in metrics)
    if missing:
        raise SmokeCheckError(
            f"/metrics is missing required metric names: {', '.join(missing)}."
        )


def run_smoke(arguments: SmokeArguments) -> RerankResponse:
    timeout = httpx.Timeout(
        connect=5.0,
        read=arguments.timeout_seconds,
        write=10.0,
        pool=10.0,
    )
    with httpx.Client(base_url=arguments.base_url, timeout=timeout) as client:
        _validate_healthcheck(
            _request_json(
                client,
                "GET",
                "/healthcheck",
                response_model=HealthcheckResponse,
            )
        )
        rerank = _validate_rerank(
            _request_json(
                client,
                "POST",
                "/rerank",
                response_model=RerankResponse,
                request_body={
                    "user_id": arguments.user_id,
                    "video_ids": list(arguments.video_ids),
                },
            ),
            requested_video_ids=arguments.video_ids,
            expected_count=arguments.expected_count,
            expected_model_id=arguments.expected_model_id,
        )
        _validate_metrics(_request_metrics(client))
    return rerank


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = parse_arguments(argv)
        rerank = run_smoke(arguments)
    except (SmokeCheckError, ValueError) as error:
        print(f"[FAIL] {error}", file=sys.stderr)
        return 1

    model_id = rerank.items[0].model_id if rerank.items else ""
    print(
        json.dumps(
            {
                "status": "ok",
                "endpoint": arguments.base_url,
                "requested_video_count": len(arguments.video_ids),
                "response_video_count": len(rerank.items),
                "model_id": model_id,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
