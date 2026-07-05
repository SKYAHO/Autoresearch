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
import json
import logging
from typing import NamedTuple, Callable

from tenacity import (
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

logger = logging.getLogger(__name__)


class Verdict(enum.Enum):
    """에러 1건에 대한 분류 결과(_classify_error 가 반환)."""

    BACKOFF = "backoff"  # 일시적 — 같은 Key backoff 후 재시도
    ROTATE = "rotate"  # Key 자체 무효/만료 — 다음 Key 회전
    TERMINAL_QUOTA = "terminal_quota"  # 프로젝트 일일 쿼터 — 즉시 skip
    TERMINAL_CONFIG = (
        "terminal_config"  # 프로젝트 설정(accessNotConfigured) — 즉시 skip
    )
    IP_BAN_CANDIDATE = "ip_ban_candidate"  # 기타 403 — IP밴 시그니처 후보


# TERMINAL_QUOTA/TERMINAL_CONFIG/ROTATE 로 직접 매핑되는 reason 집합.
# 이 집합에 없는 403 은 IP_BAN_CANDIDATE 로 떨어진다(설계 §5.2 엣지 규칙).
_QUOTA_REASONS = frozenset({"quotaExceeded", "dailyLimitExceeded"})
_CONFIG_REASONS = frozenset({"accessNotConfigured"})
_ROTATE_REASONS = frozenset({"keyInvalid", "keyExpired"})
_AUTH_REASONS = frozenset({"unauthorized", "authError", "required", "expired"})
# rateLimit 계열 — 보수적으로 BACKOFF(회전 무효).
_RATELIMIT_REASONS = frozenset(
    {
        "userRateLimitExceeded",
        "rateLimitExceeded",
        "servingLimitExceeded",
        "concurrentLimitExceeded",
        "limitExceeded",
    }
)


class _RetryableHttpError(Exception):
    """내부용: googleapiclient/requests 예외를 정규화한 재시도 후보 에러.

    status/reason 을 가지며, _retry_with_backoff 가 tenacity 로 재시도하고
    _classify_error 가 verdict 로 분류한다.
    """

    def __init__(
        self, status: int, reason: str | None, original: Exception | None = None
    ):
        self.status = status
        self.reason = reason
        self.original = original
        super().__init__(f"status={status} reason={reason}")


def _parse_reason_from_content(content: bytes) -> str | None:
    """JSON 본문 error.errors[0].reason 파싱. 실패 시 None."""
    if not content:
        return None
    try:
        body = json.loads(content)
        errors = body.get("error", {}).get("errors", [])
        if errors:
            return errors[0].get("reason")
    except Exception:
        pass
    return None


def _try_wrap_http_error(exc: Exception) -> _RetryableHttpError | None:
    """googleapiclient.errors.HttpError / requests HTTPError → _RetryableHttpError.

    변환 불가한 예외(DNS gaierror 등)는 None 반환(현재 구현에서는 그대로 전파).
    네트워크 예외(gaierror/SSLError/Timeout/Connection)는 별도 래핑(Task 4 보강).
    """
    # googleapiclient HttpError — resp.status + content JSON.
    resp = getattr(exc, "resp", None)
    if resp is not None and hasattr(resp, "status"):
        status = resp.status
        reason = _parse_reason_from_content(getattr(exc, "content", b""))
        return _RetryableHttpError(status, reason, exc)
    # requests HTTPError — response.status_code + JSON.
    response = getattr(exc, "response", None)
    if response is not None and hasattr(response, "status_code"):
        status = response.status_code
        reason = _parse_reason_from_content(getattr(response, "content", b""))
        return _RetryableHttpError(status, reason, exc)
    # 네트워크 계층(gaierror/SSLError/Timeout/Connection) — 599 가상 코드로 일시적 취급.
    net_types = ("gaierror", "SSLError", "Timeout", "Connection", "ConnectionError")
    if any(t in type(exc).__name__ for t in net_types):
        return _RetryableHttpError(599, None, exc)
    return None


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


def _is_backoff_error(exc: BaseException) -> bool:
    """tenacity retry predicate — BACKOFF 판정된 _RetryableHttpError 만 재시도.

    ROTATE/TERMINAL_*/IP_BAN_CANDIDATE 는 tenacity 가 재시도하면 안 된다.
    즉시 상위 _call_with_resilience 외곽 루프로 승격시켜야 한다
    (회전·터미널 판정은 외곽 루프의 책임). 이 함수가 False 면 tenacity 는
    reraise=True 정책에 따라 즉시 예외를 다시 던진다.
    """
    if isinstance(exc, _RetryableHttpError):
        return _classify_error(exc.status, exc.reason) is Verdict.BACKOFF
    return False


class CollectionExhausted(Exception):
    """모든 Key·경로마저 실패한 최종 폭주 상태. DAG 가 잡아 skip+알림으로 승격."""


class YouTubeCallables(NamedTuple):
    """make_callables 반환 — 순서 실수를 타입으로 방지(fetch.py 계약)."""

    list_videos: Callable[..., dict]
    list_channels: Callable[..., dict]
    list_categories: Callable[..., dict]


# 정상 경로(googleapiclient 직접) callable 생산 팩토리 — 테스트 주입점.
# 기본 구현은 실제 googleapiclient.build 를 쓰고, 단위테스트는 가짜 팩토리를 주입.
ServiceCallables = tuple[Callable[..., dict], Callable[..., dict], Callable[..., dict]]


def _default_service_factory(api_key: str) -> ServiceCallables:
    """googleapiclient 로 실제 service 를 만들어 fetch.py 용 callable 로 adapt.

    DAG 는 이 함수를 직접 쓰지 않고 ResilientYouTubeClient 에게 넘긴다.
    단위테스트는 가짜 팩토리를 _service_factory 인자로 주입해 googleapiclient 를 孤立시킨다.
    """
    from googleapiclient.discovery import build

    service = build("youtube", "v3", developerKey=api_key, cache_discovery=False)
    return (
        lambda **kw: service.videos().list(**kw).execute(),
        lambda **kw: service.channels().list(**kw).execute(),
        lambda **kw: service.videoCategories().list(**kw).execute(),
    )


class ResilientYouTubeClient:
    """YouTube 수집 복원력 클라이언트.

    fetch.collect_trending 이 기대하는 3개 callable(list_videos/list_channels/
    list_categories)를 생산하되, 각 호출에 계층적 복원력을 입힌다.

    상태(무효화된 Key, IP밴 시그니처 이력, Circuit Breaker)는 per-run
    (이 인스턴스) 공유. 매일 새 DAG run = 새 인스턴스 = 자동 리셋.

    Args:
        keys: API Key 리스트. 최소 1개. Key 1개면 회전 불가(IP밴 시그니처도 불성립).
        proxy_url: Cloud Run 프록시 URL. 1차 PR 기본 None(비활성).
        max_retries: tenacity — 현재 Key+경로 조합에 대한 backoff 최대 시도.
        max_proxy_attempts: 프록시 경로 재시도 상한. proxy_url=None 이면 미사용.
        max_total_calls: 한 collection run 폭주 가드(총 호출 수 상한).
        _service_factory: 테스트 주입용(기본 _default_service_factory).
    """

    def __init__(
        self,
        keys: list[str],
        *,
        proxy_url: str | None = None,
        max_retries: int = 3,
        max_proxy_attempts: int = 2,
        max_total_calls: int = 60,
        _service_factory: Callable[[str], ServiceCallables] = _default_service_factory,
    ):
        if not keys:
            raise ValueError("keys 는 최소 1개 필요")
        self._keys = list(keys)
        self._proxy_url = proxy_url
        self._max_retries = max_retries
        self._max_proxy_attempts = max_proxy_attempts
        self._max_total_calls = max_total_calls
        self._service_factory = _service_factory
        # per-run 상태
        self._invalid_keys: set[str] = set()
        self._call_count = 0
        # Circuit Breaker (per-run). CLOSED/OPEN 이항(매일 새 인스턴스로 리셋).
        self._breaker_open: bool = False
        # IP밴 시그니처 — 현재 자원 호출 라운드에서 IP_BAN_CANDIDATE 누적.
        # key → reason. _invalid_keys 와 분리(시그니처 판정이 활성 Key 전체를 봐야 하므로).
        self._ip_ban_candidates: dict[str, str] = {}
        self._proxy_attempts: int = 0

    def make_callables(self) -> YouTubeCallables:
        """fetch.collect_trending 용 (list_videos, list_channels, list_categories).

        각 callable 은 (**kw) -> dict. 복원력 로직은 내부에서 자원 종류별로
        동일하게 적용된다.
        """
        return YouTubeCallables(
            list_videos=self._make_resilient_callable("videos"),
            list_channels=self._make_resilient_callable("channels"),
            list_categories=self._make_resilient_callable("categories"),
        )

    def _make_resilient_callable(self, resource: str) -> Callable[..., dict]:
        """자원별 복원력 callable. tenacity 안쪽 + 외곽 회전/터미널 루프."""

        def resilient(**kw) -> dict:
            return self._call_with_resilience(resource, kw)

        return resilient

    def _call_with_resilience(self, resource: str, kw: dict) -> dict:
        """외곽 루프: Key 회전 + 터미널 판정. tenacity는 안쪽."""
        if self._breaker_open:
            # 이전 호출에서 IP밴 시그니처 확정 → 이후 자원 호출 즉시 skip.
            raise CollectionExhausted(
                f"Circuit Breaker OPEN resource={resource} (이전 호출에서 폭주 확정)"
            )
        # 자원 호출 라운드 시작 — IP밴 시그니처 누적 초기화(per resource 라운드).
        self._ip_ban_candidates = {}
        last_verdict: Verdict | None = None
        while True:
            key = self._pick_active_key()
            if key is None:
                raise CollectionExhausted(
                    f"활성 Key 없음 (resource={resource}, last_verdict={last_verdict})"
                )
            list_callable = self._get_list_callable(key, resource)
            try:
                # tenacity 안쪽: 현재 Key+경로에 대해 backoff.
                return self._retry_with_backoff(list_callable, kw)
            except _RetryableHttpError as e:
                # tenacity 가 backoff 소진하고 던진 예외 — verdict 로 분기.
                verdict = _classify_error(e.status, e.reason)
                self._log_decision(resource, key, "normal", verdict, e)
                last_verdict = verdict
                if verdict is Verdict.BACKOFF:
                    # backoff 소진 후에도 지속 — 일시 장애 skip(전환/회전 없이).
                    raise CollectionExhausted(
                        f"일시 장애 backoff 소진 resource={resource} "
                        f"status={e.status} reason={e.reason}"
                    )
                if verdict is Verdict.ROTATE:
                    # 현재 Key 무효화(keyInvalid/keyExpired/401) — 즉시 다음 Key 회전.
                    self._invalid_keys.add(key)
                    continue  # while 재시도 → _pick_active_key 가 다음 활성 Key 반환
                if verdict is Verdict.TERMINAL_QUOTA:
                    # 프로젝트 일일 쿼터 소진 — Key 회전 무효(전 Key 동일). 즉시 skip 승격.
                    raise CollectionExhausted(
                        f"프로젝트 일일 쿼터 소진 — 회전 무효 resource={resource} reason={e.reason}"
                    )
                if verdict is Verdict.TERMINAL_CONFIG:
                    # accessNotConfigured — 프로젝트 스코프 설정 문제. 회전 무효. 즉시 skip.
                    raise CollectionExhausted(
                        f"프로젝트 설정 문제(accessNotConfigured) — 회전 무효 "
                        f"resource={resource}"
                    )
                if verdict is Verdict.IP_BAN_CANDIDATE:
                    # IP밴 시그니처 판정 — 전 활성 Key 동일 403 인지 검사.
                    sig = self._record_ip_ban_candidate(key, resource, e.reason)
                    if sig is Verdict.IP_BAN_CANDIDATE:
                        # 시그니처 성립 → Circuit Breaker OPEN.
                        self._breaker_open = True
                        if self._proxy_url is None:
                            raise CollectionExhausted(
                                f"IP 밴 시그니처 감지(전 Key 동일 403), "
                                f"프록시 미구성으로 skip resource={resource}"
                            )
                        # proxy_url 있음 → 프록시 경로 전환(1차: stub, 2차에서 구현).
                        return self._call_via_proxy(resource, kw)
                    # 시그니처 미성립 → 다음 Key 회전.
                    # NOTE: _invalid_keys 가 아닌 _ip_ban_candidates 로 회전 추적.
                    # _invalid_keys 에 넣으면 시그니처 판정의 active_keys 계산이
                    # 깨지므로(후보 Key 가 빠져 len<2 가 됨). _pick_active_key 가
                    # _ip_ban_candidates 의 Key 도 스킵하도록 분리.
                    continue
                raise CollectionExhausted(
                    f"verdict={verdict} resource={resource} status={e.status} reason={e.reason}"
                )

    def _retry_with_backoff(self, list_callable: Callable[..., dict], kw: dict) -> dict:
        """tenacity — BACKOFF 분류 에러만 재시도. 다른 verdict는 즉시 예외로 승격.

        ROTATE/TERMINAL/IP_BAN_CANDIDATE 판정 에러는 tenacity 가 재시도하지
        않고(see _is_backoff_error) 즉시 _RetryableHttpError 를 다시 던져
        상위 _call_with_resilience 외곽 루프가 회전·터미널 판정을 내리게 한다.
        """
        for attempt in Retrying(
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential_jitter(initial=1, max=30),
            retry=retry_if_exception(_is_backoff_error),
            reraise=True,
        ):
            with attempt:
                self._check_call_budget()
                self._call_count += 1
                try:
                    return list_callable(**kw)
                except _RetryableHttpError:
                    raise  # tenacity 가 재시도
                except Exception as e:
                    # googleapiclient HttpError → _RetryableHttpError 로 변환 시도.
                    retryable = _try_wrap_http_error(e)
                    if retryable is not None:
                        raise retryable
                    raise  # 변환 불가 — 그대로 전파(네트워크 예외 등은 아래 추가 래핑)
        # 도달 불가(reraise=True) — 정적 분석용.
        raise RuntimeError("unreachable")

    def _log_decision(
        self,
        resource: str,
        key: str,
        route: str,
        verdict: Verdict,
        exc: Exception,
    ) -> None:
        """관측성 로그. Key 값/헤더/본문/traceback 절대 기록 안 함(key_index 만)."""
        logger.warning(
            "youtube resilience decision resource=%s key_index=%d route=%s "
            "verdict=%s status=%s reason=%s",
            resource,
            self._key_index(key),
            route,
            verdict.value,
            getattr(exc, "status", "?"),
            getattr(exc, "reason", "?"),
        )

    def _pick_active_key(self) -> str | None:
        """무효화되지 않은 + 이번 라운드에서 아직 시도 안 한 첫 Key. 없으면 None.

        _invalid_keys(ROTATE 등 영구 무효)와 _ip_ban_candidates(이번 라운드
        IP_BAN_CANDIDATE 로 시도된 Key) 모두 스킵. 후자를 분리한 이유는
        시그니처 판정이 활성 Key 전체를 봐야 하므로 _invalid_keys 에 넣으면
        active_keys 계산이 깨지기 때문.
        """
        for k in self._keys:
            if k in self._invalid_keys:
                continue
            if k in self._ip_ban_candidates:
                continue
            return k
        return None

    def _key_index(self, key: str) -> int:
        """로깅용 Key 식별자(값 아님). 0-base."""
        return self._keys.index(key)

    def _get_list_callable(self, key: str, resource: str) -> Callable[..., dict]:
        """현재 Key·경로에 해당하는 fetch.py 용 callable. Task 3는 정상 경로만."""
        list_videos, list_channels, list_categories = self._service_factory(key)
        return {
            "videos": list_videos,
            "channels": list_channels,
            "categories": list_categories,
        }[resource]

    def _check_call_budget(self) -> None:
        """max_total_calls 폭주 가드. 호출 수 누적 초과 시 CollectionExhausted.

        _retry_with_backoff 가 매 시도 전(증가 전) 호출. 따라서
        `_call_count >= _max_total_calls` 일 때 즉시 폭주 확정 — 무효 Key 반복
        루프나 IP밴 시그니처 실패 폭주 등으로 호출 수가 풀리지 못하는 상황을
        per-run 총량으로 막는다. Key 값/헤더/본문은 기록하지 않는다
        (CollectionExhausted 메시지에도 call_count 정수만).
        """
        if self._call_count >= self._max_total_calls:
            raise CollectionExhausted(
                f"폭주 가드: max_total_calls={self._max_total_calls} 도달 "
                f"(call_count={self._call_count})"
            )

    def _record_ip_ban_candidate(
        self, key: str, resource: str, reason: str | None
    ) -> None | Verdict:
        """IP밴 시그니처 판정. None=계속 회전, Verdict.IP_BAN_CANDIDATE=터미널 판정.

        엣지 규칙(설계 §5.2):
        - reason 파싱 실패(None) → 시그니처 미카운트. 단 회전 추적용 sentinel("")
          으로 마킹하여 _pick_active_key 가 같은 key 를 반복 선택하지 않게 함
          (tight loop 방지). 시그니처 판정에서는 빈 reason 후보 제외.
        - 활성 Key ≥2 일 때만 시그니처 인정.
        - 모든 활성 Key 가 candidate 이고 동일 reason 일 때 성립.
        - 부분 성공(일부 Key 만 candidate) → 불성립.

        활성 Key 계산에서 _ip_ban_candidates 자체는 제외하지 않음(이번 라운드
        후보도 여전히 활성으로 쳐야 전원 동일 실패 여부를 검사 가능).
        _invalid_keys(ROTATE 영구 무효)만 제외.
        """
        if reason is None:
            # reason 파싱 실패 → 시그니처 미카운트이나, 회전 추적용 sentinel 마킹.
            # 이로써 _pick_active_key 가 같은 key 를 반복 선택하는 tight loop 방지.
            self._ip_ban_candidates[key] = ""
            return None
        self._ip_ban_candidates[key] = reason
        active_keys = [k for k in self._keys if k not in self._invalid_keys]
        if len(active_keys) < 2:
            # 최소 Key≥2 아니면 시그니처 불성립. 회전 계속(결국 소진).
            return None
        # 빈 reason(sentinel) 후보는 시그니처 판정에서 제외(엣지: reason=None 미카운트).
        candidates_with_reason = {k: v for k, v in self._ip_ban_candidates.items() if v}
        # 모든 활성 Key 가 이미 candidate 인가?(부분 성공이면 일부만 candidate)
        if all(k in candidates_with_reason for k in active_keys):
            # 동일 reason 인가?(엣지: reason 정확히 같아야)
            reasons = {candidates_with_reason[k] for k in active_keys}
            if len(reasons) == 1:
                # 시그니처 성립.
                return Verdict.IP_BAN_CANDIDATE
        return None

    def _call_via_proxy(self, resource: str, kw: dict) -> dict:
        """프록시 경로 호출. 1차 PR 은 프록시 미배포 → 시그니처 후 즉시 CollectionExhausted.

        2차 PR 에서 requests.get(proxy_url/youtube/v3/...) 로 구현.
        """
        raise CollectionExhausted(
            f"프록시 경로 미구현(1차 PR) resource={resource} proxy_url={self._proxy_url}"
        )
