"""YouTube Data API v3 수집용 복원력 클라이언트(daily DAG 가 사용).

기존 fetch.py 의 callable 주입 seam(service.videos().list().execute() 를
list_videos(**kw) 로 adapt)을 그대로 활용하되, 그 callable 생산층에
계층적 복원력을 끼워넣는다:

    재시도(tenacity, 안쪽) → Key 롤링(keyInvalid/keyExpired/401) →
    IP밴 시그니처(전 Key 동일 403) → Circuit Breaker → skip + 알림

설계 문서: docs/superpowers/specs/2026-07-03-youtube-ip-ban-resilience-design.md
이슈 #47.
"""
from __future__ import annotations

import enum
import logging
from typing import NamedTuple, Callable

logger = logging.getLogger(__name__)


class Verdict(enum.Enum):
    """에러 1건에 대한 분류 결과(_classify_error 가 반환)."""

    BACKOFF = "backoff"                  # 일시적 — 같은 Key backoff 후 재시도
    ROTATE = "rotate"                    # Key 자체 무효/만료 — 다음 Key 회전
    TERMINAL_QUOTA = "terminal_quota"    # 프로젝트 일일 쿼터 — 즉시 skip
    TERMINAL_CONFIG = "terminal_config"  # 프로젝트 설정(accessNotConfigured) — 즉시 skip
    IP_BAN_CANDIDATE = "ip_ban_candidate"  # 기타 403 — IP밴 시그니처 후보


# TERMINAL_QUOTA/TERMINAL_CONFIG/ROTATE 로 직접 매핑되는 reason 집합.
# 이 집합에 없는 403 은 IP_BAN_CANDIDATE 로 떨어진다(설계 §5.2 엣지 규칙).
_QUOTA_REASONS = frozenset({"quotaExceeded", "dailyLimitExceeded"})
_CONFIG_REASONS = frozenset({"accessNotConfigured"})
_ROTATE_REASONS = frozenset({"keyInvalid", "keyExpired"})
_AUTH_REASONS = frozenset({"unauthorized", "authError", "required", "expired"})
# rateLimit 계열 — 보수적으로 BACKOFF(회전 무효).
_RATELIMIT_REASONS = frozenset({
    "userRateLimitExceeded",
    "rateLimitExceeded",
    "servingLimitExceeded",
    "concurrentLimitExceeded",
    "limitExceeded",
})


def _classify_error(status: int, reason: str | None) -> Verdict:
    """YouTube API 응답(status + error.errors[].reason) → Verdict.

    Args:
        status: HTTP 상태코드.
        reason: 본문 error.errors[0].reason. 파싱 실패/누락 시 None/빈문자열.

    분류 우선순위(reason 이 명확할 때):
        1. 쿼터 계열(quotaExceeded/dailyLimitExceeded) → TERMINAL_QUOTA
        2. accessNotConfigured → TERMINAL_CONFIG
        3. Key 무효/만료(keyInvalid/keyExpired/401 인증) → ROTATE
        4. rateLimit 계열 → BACKOFF(보수적 회전 무효)
        5. 5xx → BACKOFF(일시적)
        6. 403(위 어디에도 해당하지 않음, reason 유불문) → IP_BAN_CANDIDATE
           — reason 이 빈/불명인 403 이 YouTube IP밴 시그니처의 본체(설계 §5.2 엣지).
        7. 그 외(reason 없는 4xx 등) → BACKOFF(일시적 기본정책)

    주의: brief 원본 코드는 `not reason → BACKOFF` 를 `status==403` 검사보다
    먼저 두어 (403, "") 이 BACKOFF 로 떨어졌으나, 같은 brief 의 parametrized
    테스트가 (403, "") → IP_BAN_CANDIDATE 를 요구한다(TDD: 테스트가 명세).
    따라서 403 검사를 empty-reason fallback 보다 먼저 둔다.
    """
    reason = reason or ""

    if reason in _QUOTA_REASONS:
        return Verdict.TERMINAL_QUOTA
    if reason in _CONFIG_REASONS:
        return Verdict.TERMINAL_CONFIG
    if reason in _ROTATE_REASONS:
        return Verdict.ROTATE
    if status == 401 or reason in _AUTH_REASONS:
        return Verdict.ROTATE
    if reason in _RATELIMIT_REASONS:
        return Verdict.BACKOFF
    if status >= 500:
        return Verdict.BACKOFF
    # 403 계열 중 위 분류 어디에도 해당하지 않는 "기타 403" — reason 빈/불명 포함.
    # reason 이 없는 403 이 IP밴 시그니처의 본체이므로 empty-reason fallback 보다 먼저.
    if status == 403:
        return Verdict.IP_BAN_CANDIDATE
    # 그 이유를 알 수 없는 4xx — 일시적 기본 정책.
    return Verdict.BACKOFF
