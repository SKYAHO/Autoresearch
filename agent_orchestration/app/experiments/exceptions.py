"""Agent Orchestration 실험 도메인 오류를 정의한다.

전체 파이프라인에서 실험 service가 감지한 not-found·멱등성 충돌을 HTTP 계층과 분리해
표현한다. FastAPI 상태 코드 변환과 DB 예외 로깅은 담당하지 않는다.
"""

from __future__ import annotations

import uuid


class ExperimentNotFoundError(LookupError):
    """요청한 UUID의 실험이 존재하지 않는다."""

    def __init__(self, experiment_id: uuid.UUID) -> None:
        self.experiment_id = experiment_id
        super().__init__(f"Experiment '{experiment_id}' was not found.")


class InvalidCursorError(LookupError):
    """polling cursor(after_id)가 가리키는 row가 존재하지 않는다."""

    def __init__(self, after_id: uuid.UUID) -> None:
        self.after_id = after_id
        super().__init__(f"Cursor '{after_id}' was not found.")


class IdempotencyConflictError(ValueError):
    """같은 멱등성 key가 서로 다른 payload에 재사용됐다."""

    def __init__(self, idempotency_key: str) -> None:
        self.idempotency_key = idempotency_key
        super().__init__(
            f"Idempotency key '{idempotency_key}' was already used with another payload."
        )


class PromotionRequiresDedicatedEndpointError(ValueError):
    """일반 상태 API가 PROMOTED 전이를 요청했다."""

    def __init__(self) -> None:
        super().__init__("PROMOTED must be requested through the promotion endpoint.")


class IssuePublicationLimitError(RuntimeError):
    """일일 발행 상한을 넘었다."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        super().__init__(f"Daily issue publication limit {limit} was reached.")
