# 유튜브 수집 복원력 client.py 구현 (1차) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `ResilientYouTubeClient` 신규 모듈로 YouTube 수집에 계층적 복원력(재시도 → Key 롤링 → IP밴 시그니처 → Circuit Breaker → skip)을 추가하고, 일일 DAG를 이 클라이언트로 교체한다.

**Architecture:** 기존 `fetch.py`의 callable 주입 seam을 활용 — `fetch.py`는 변경 없이, `service.videos().list().execute()`를 생산하던 `_make_callables`를 `ResilientYouTubeClient.make_callables()`로 교체. tenacity는 가장 안쪽(현재 Key+경로에만 backoff), 회전/전환은 외곽 루프, 상태는 per-run(클라이언트 인스턴스) 공유. 1차 PR은 `proxy_url=None`(프록시 경로 비활성, IP밴 감지 시 즉시 skip).

**Tech Stack:** Python 3.11/3.12, tenacity 9.x, requests 2.32.x, pydantic 2.6+, Apache Airflow 2.10.x(astro-runtime 13.8.0), google-api-python-client 2.100+, pytest.

## Global Constraints

- Python 3.11/3.12 양쪽에서 pytest 통과 (CI 매트릭스).
- Airflow 2.10.x 기준. `AirflowFailException`은 `from airflow.exceptions import AirflowFailException` (3.x 마이그레이션 시 재검증).
- **`fetch.py`는 변경 금지** (기존 단위테스트 보존, 회귀 없음 확인).
- 한국어 docstring/주석, 영어 식별자(기존 `fetch.py` 컨벤션 준수).
- 커밋 메시지: `<type>: <한국어 설명>` (feat/fix/docs/refactor/test/chore).
- TDD: 각 기능별로 실패 테스트 → 구현 → 통과 → 커밋.
- **로깅 마스킹**: Key 값, `X-Goog-Api-Key`/Authorization 헤더, 응답 본문 전체, raw 예외 traceback은 절대 로그에 넣지 않는다. `key_index`(식별자만)를 사용.
- `proxy_url=None`이 1차 기본값 (프록시 경로 비활성).
- 사전 승인된 이슈: #47. 브랜치 `feat/47-youtube-resilient-client` (이슈 먼저 발행됨 가정).

---

## File Structure

```
autoresearch/youtube_collection/
  client.py        ← 신규: ResilientYouTubeClient, CollectionExhausted, YouTubeCallables,
                      Verdict(enum), _classify_error(순수 함수)
  fetch.py         (변경 없음)
  transform.py / load.py / schema.py / backfill.py (변경 없음)

tests/
  test_youtube_client.py   ← 신규: 16개 시나리오 단위테스트 (callable 주입)

dags/youtube_trending_kr_daily.py  ← 수정: _build_service/_make_callables 제거 →
                                       ResilientYouTubeClient.make_callables() 사용 +
                                       CollectionExhausted → AirflowFailException 승격

.env.example       ← 수정: YOUTUBE_API_KEYS(복수)/YOUTUBE_PROXY_URL 추가
.gitignore         ← 수정: .env* 패턴
requirements.txt   ← 수정: tenacity, requests 추가 + 상한 핀

.github/workflows/ci.yml  ← (권장) 보강: pip-audit/gitleaks 스텝
```

---

## Task 1: 의존성 + 설정 위생 동기화

**Files:**
- Modify: `requirements.txt`
- Modify: `.env.example`
- Modify: `.gitignore`
- Test: `tests/test_youtube_client.py`(이후 태스크에서 작성, 여기서는 파일 생성만)

**Interfaces:**
- Consumes: 없음 (독립 설정 변경)
- Produces: `tenacity`, `requests` 의존성 가용; `YOUTUBE_API_KEYS`/`YOUTUBE_PROXY_URL` 환경변수 계약; `.env*` gitignore 패턴.

- [ ] **Step 1: requirements.txt 수정**

````
# YouTube 트렌딩 수집(Airflow/Astro) 런타임 의존성.
# 테스트/실험 모듈 의존성은 requirements-dev.txt 참조.
pydantic>=2.6,<3
pyarrow>=15.0
google-api-python-client>=2.100,<3
google-cloud-storage>=2.10
gcsfs>=2024.0
tenacity>=8.2,<10
requests>=2.32,<3
````

- [ ] **Step 2: .env.example 수정**

````
# YouTube Data API v3 developer keys (쉼표 구분 복수; Key 무효화 대응)
YOUTUBE_API_KEYS=
# (deprecated) 단수 Key — YOUTUBE_API_KEYS 가 우선.
YOUTUBE_API_KEY=

# GCS bucket name for the data lake (no gs:// prefix)
YOUTUBE_LAKE_BUCKET=

# Kaggle global parquet source for the backfill DAG (gs:// or local path)
YOUTUBE_BACKFILL_SOURCE=

# Cloud Run 프록시 URL (1차: 미설정=비활성. 2차 PR에서 주입)
YOUTUBE_PROXY_URL=
````

- [ ] **Step 3: .gitignore 수정 (.env 변형 패턴 추가)**

현재 `.env` 한 줄만 있음. 아래로 교체/추가:

````
__pycache__/
*.py[cod]

# Astro / Airflow local state
.astro/
.env
.env.*
!.env.example
.gcp-creds.json

# Generated data artifacts
data/generated/
/tmp/
````

- [ ] **Step 4: 의존성 설치 후 import 확인**

Run: `pip install -r requirements.txt && python -c "import tenacity, requests; print(tenacity.__version__, requests.__version__)"`
Expected: 버전 출력 (tenacity 8.x/9.x, requests 2.32.x)

- [ ] **Step 5: 기존 테스트 회귀 확인**

Run: `pytest tests/ -v`
Expected: 전체 PASS (설정 변경이 로직에 영향 없음)

- [ ] **Step 6: Commit**

```bash
git add requirements.txt .env.example .gitignore
git commit -m "chore: 1차 PR 설정 위생 동기화 (tenacity/requests 추가, 복수 Key/프록시 URL 환경변수, .env* gitignore)"
```

---

## Task 2: 에러 분류 순수 함수 (`_classify_error` + `Verdict`)

**Files:**
- Create: `autoresearch/youtube_collection/client.py` (초기 뼈대 + 분류 함수만)
- Test: `tests/test_youtube_client.py`

**Interfaces:**
- Consumes: 없음
- Produces: `Verdict`(enum), `_classify_error(status, reason) -> Verdict` (순수 함수, 부작용 없음). 이후 태스크의 분류 규칙의 단일 진실 공급원.

**Verdict 정의 (설계 §5.2 분류표 매핑):**
- `BACKOFF`: 일시적(같은 Key backoff 후 재시도). 5xx/네트워크/DNS/SSL, `userRateLimitExceeded`, `rateLimitExceeded`, `servingLimitExceeded`/`concurrentLimitExceeded`/`limitExceeded`, malformed/unknown 기본.
- `ROTATE`: Key 자체 무효/만료. `keyInvalid`/`keyExpired`, 401 인증 계열.
- `TERMINAL_QUOTA`: 프로젝트 일일 쿼터 소진. `quotaExceeded`/`dailyLimitExceeded`.
- `TERMINAL_CONFIG`: 프로젝트 스코프 설정 문제. `accessNotConfigured`.
- `IP_BAN_CANDIDATE`: 403 계열이되 위 분류 어디에도 해당하지 않는 "기타 403". IP밴 시그니처 후보(설계 §5.2 마지막 행 + 엣지 규칙).

- [ ] **Step 1: 실패 테스트 작성 (분류표 각 행)**

`tests/test_youtube_client.py`:

```python
import pytest

from autoresearch.youtube_collection.client import Verdict, _classify_error


@pytest.mark.parametrize(
    "status,reason,expected",
    [
        # TERMINAL_QUOTA — 프로젝트 단위
        (403, "quotaExceeded", Verdict.TERMINAL_QUOTA),
        (403, "dailyLimitExceeded", Verdict.TERMINAL_QUOTA),
        # userRateLimitExceeded — 보수적 회전 무효(같은 Key backoff)
        (403, "userRateLimitExceeded", Verdict.BACKOFF),
        # API/글로벌 레이트리밋
        (403, "rateLimitExceeded", Verdict.BACKOFF),
        (429, "rateLimitExceeded", Verdict.BACKOFF),
        (403, "servingLimitExceeded", Verdict.BACKOFF),
        (403, "concurrentLimitExceeded", Verdict.BACKOFF),
        (403, "limitExceeded", Verdict.BACKOFF),
        # ROTATE — Key 자체 무효/만료
        (400, "keyInvalid", Verdict.ROTATE),
        (400, "keyExpired", Verdict.ROTATE),
        (401, "unauthorized", Verdict.ROTATE),
        (401, "authError", Verdict.ROTATE),
        (401, "required", Verdict.ROTATE),
        (401, "expired", Verdict.ROTATE),
        # TERMINAL_CONFIG — 프로젝트 스코프
        (403, "accessNotConfigured", Verdict.TERMINAL_CONFIG),
        # 5xx / 네트워크 → 일시적
        (500, "internalError", Verdict.BACKOFF),
        (503, "backendError", Verdict.BACKOFF),
        (503, "notReady", Verdict.BACKOFF),
        # malformed/unknown → 일시적 기본
        (503, None, Verdict.BACKOFF),
        (403, "someUndefinedReason", Verdict.IP_BAN_CANDIDATE),  # 기타 403
        (403, "", Verdict.IP_BAN_CANDIDATE),  # 빈 reason 403
    ],
)
def test_classify_error_maps_youtube_reasons(status, reason, expected):
    assert _classify_error(status, reason) is expected
```

- [ ] **Step 2: 테스트 실행 — 실패 확인**

Run: `pytest tests/test_youtube_client.py::test_classify_error_maps_youtube_reasons -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'autoresearch.youtube_collection.client'`

- [ ] **Step 3: client.py 초기 뼈대 + 분류 함수 구현**

`autoresearch/youtube_collection/client.py`:

```python
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
        6. reason 없/불명/빈 → BACKOFF(일시적 기본정책)
        7. 위 어디에도 해당하지 않는 403 → IP_BAN_CANDIDATE
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
    if not reason:
        # reason 파싱 실패/누락 — 일시적 기본 정책(설계 §5.2 엣지: 시그니처 미카운트).
        return Verdict.BACKOFF
    # 403 계열 중 위 분류 어디에도 해당하지 않는 "기타 403".
    if status == 403:
        return Verdict.IP_BAN_CANDIDATE
    # 그 외(4xx) — 일시적 기본.
    return Verdict.BACKOFF
```

- [ ] **Step 4: 테스트 실행 — 통과 확인**

Run: `pytest tests/test_youtube_client.py::test_classify_error_maps_youtube_reasons -v`
Expected: 전체 PASS

- [ ] **Step 5: 기존 테스트 회귀 확인**

Run: `pytest tests/ -v`
Expected: 전체 PASS (client.py 신규 모듈이 기존에 영향 없음)

- [ ] **Step 6: Commit**

```bash
git add autoresearch/youtube_collection/client.py tests/test_youtube_client.py
git commit -m "feat: YouTube 에러 분류 순수 함수 추가 (Verdict + _classify_error)"
```

---

## Task 3: `ResilientYouTubeClient` 뼈대 + 정상 경로

**Files:**
- Modify: `autoresearch/youtube_collection/client.py`
- Test: `tests/test_youtube_client.py`

**Interfaces:**
- Consumes: `Verdict`, `_classify_error` (Task 2)
- Produces: `CollectionExhausted`(예외), `YouTubeCallables`(NamedTuple), `ResilientYouTubeClient(keys, *, proxy_url, max_retries, max_proxy_attempts, max_total_calls)`, `ResilientYouTubeClient.make_callables() -> YouTubeCallables`. 정상 경로: `make_callables()`가 반환한 각 callable이 `(**kw) -> dict` 계약.

- [ ] **Step 1: 실패 테스트 작성 (정상 경로 — Key 1개, 정상 응답)**

`tests/test_youtube_client.py` 에 추가:

```python
from autoresearch.youtube_collection.client import (
    ResilientYouTubeClient,
    YouTubeCallables,
)


def _fake_videos_response():
    return {"items": [{"id": "v1"}], "nextPageToken": None}


def test_make_callables_returns_named_tuple_with_three_callables():
    client = ResilientYouTubeClient(keys=["k1"])

    callables = client.make_callables()

    assert isinstance(callables, YouTubeCallables)
    assert callable(callables.list_videos)
    assert callable(callables.list_channels)
    assert callable(callables.list_categories)


def test_normal_path_single_key_returns_response():
    """Key 1개, 정상 응답 — 가장 단순한 성공 경로."""
    calls = []

    def fake_service_factory(key):
        def list_videos(**kw):
            calls.append(("videos", key, kw))
            return _fake_videos_response()

        def list_channels(**kw):
            calls.append(("channels", key, kw))
            return {"items": []}

        def list_categories(**kw):
            calls.append(("categories", key, kw))
            return {"items": []}

        return list_videos, list_channels, list_categories

    client = ResilientYouTubeClient(
        keys=["k1"], _service_factory=fake_service_factory
    )
    callables = client.make_callables()

    result = callables.list_videos(part="snippet", chart="mostPopular")

    assert result == _fake_videos_response()
    assert len(calls) == 1
    assert calls[0][1] == "k1"  # Key 1개 사용
```

- [ ] **Step 2: 테스트 실행 — 실패 확인**

Run: `pytest tests/test_youtube_client.py -k "normal_path or make_callables" -v`
Expected: FAIL — `ImportError: cannot import name 'ResilientYouTubeClient'`

- [ ] **Step 3: ResilientYouTubeClient 뼈대 구현 (정상 경로)**

`client.py` 에 추가 (import 섹션 아래):

```python
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
    단위테스트는 가짜 팩토리를 _service_factory 인자로 주입해 googleapiclient 를孤立시킨다.
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
        """자원별 복원력 callable 생산. Task 3 에선 정상 경로만(복원력은 후속 태스크)."""
        def resilient(**kw) -> dict:
            key = self._pick_active_key()
            if key is None:
                raise CollectionExhausted("활성 Key 없음")
            self._check_call_budget()
            list_callable = self._get_list_callable(key, resource)
            self._call_count += 1
            logger.debug(
                "youtube call resource=%s key_index=%d route=normal",
                resource,
                self._key_index(key),
            )
            return list_callable(**kw)

        return resilient

    def _pick_active_key(self) -> str | None:
        """무효화되지 않은 첫 Key. 없으면 None."""
        for k in self._keys:
            if k not in self._invalid_keys:
                return k
        return None

    def _key_index(self, key: str) -> int:
        """로깅용 Key 식별자(값 아님). 0-base."""
        return self._keys.index(key)

    def _get_list_callable(self, key: str, resource: str) -> Callable[..., dict]:
        """현재 Key·경로에 해당하는 fetch.py 용 callable. Task 3는 정상 경로만."""
        list_videos, list_channels, list_categories = self._service_factory(key)
        return {"videos": list_videos, "channels": list_channels, "categories": list_categories}[resource]

    def _check_call_budget(self) -> None:
        """max_total_calls 폭주 가드. Task 8 에서 본격 구현, Task 3는 스텁."""
        # Task 8 에서 채움.
```

- [ ] **Step 4: 테스트 실행 — 통과 확인**

Run: `pytest tests/test_youtube_client.py -k "normal_path or make_callables" -v`
Expected: 2개 PASS

- [ ] **Step 5: 기존 테스트 회귀**

Run: `pytest tests/ -v`
Expected: 전체 PASS

- [ ] **Step 6: Commit**

```bash
git add autoresearch/youtube_collection/client.py tests/test_youtube_client.py
git commit -m "feat: ResilientYouTubeClient 뼈대 + 정상 경로 (make_callables/YouTubeCallables/CollectionExhausted)"
```

---

## Task 4: tenacity 재시도 (5xx/네트워크 backoff)

**Files:**
- Modify: `autoresearch/youtube_collection/client.py`
- Test: `tests/test_youtube_client.py`

**Interfaces:**
- Consumes: Task 3 (`_make_resilient_callable`)
- Produces: tenacity가 안쪽에서 "현재 Key+경로"에 대해 backoff. BACKOFF verdict 에러(5xx/네트워크/rateLimit)는 `max_retries`회 backoff 후 재시도, 소진 시 예외 전파(외곽 루프가 처리 — 현재는 CollectionExhausted로 승격).

**주의:** `googleapiclient.errors.HttpError` 흉내를 낸 테스트용 예외 클래스를 정의해 분류 로직에 status/reason을 전달한다.

- [ ] **Step 1: 테스트용 가짜 HttpError 헬퍼 + 실패 테스트 작성**

`tests/test_youtube_client.py` 상단에 헬퍼 추가:

```python
import json


class FakeHttpError(Exception):
    """googleapiclient.errors.HttpError 흉내. status/reason 전달용."""

    def __init__(self, status: int, reason: str | None):
        self.status = status
        self.reason = reason
        body = {"error": {"errors": [{"reason": reason or ""}]}}
        # googleapiclient HttpError 는 resp.status 와 content(JSON bytes)를 가짐.
        class _Resp:
            def __init__(self, s):
                self.status = s
        self.resp = _Resp(status)
        self.content = json.dumps(body).encode()
        super().__init__(f"FakeHttpError status={status} reason={reason}")


def _make_service_that_raises(*errors, then_return=None):
    """errors 순서대로 raise 하다가, 소진 후 then_return 반환하는 service 팩토리."""
    state = {"i": 0}

    def factory(key):
        def list_videos(**kw):
            i = state["i"]
            if i < len(errors):
                state["i"] += 1
                raise errors[i]
            return then_return or _fake_videos_response()

        return list_videos, lambda **kw: {"items": []}, lambda **kw: {"items": []}

    return factory
```

테스트 추가:

```python
def test_5xx_backoff_then_success():
    """500 → 500 → 200. tenacity backoff 후 정상 복귀."""
    factory = _make_service_that_raises(
        FakeHttpError(500, "internalError"),
        FakeHttpError(503, "backendError"),
        then_return=_fake_videos_response(),
    )
    client = ResilientYouTubeClient(keys=["k1"], max_retries=3, _service_factory=factory)
    result = client.make_callables().list_videos(part="snippet")

    assert result == _fake_videos_response()


def test_5xx_backoff_exhausted_raises_collection_exhausted():
    """500 × max_retries회 반복 → 소진 → CollectionExhausted."""
    factory = _make_service_that_raises(
        FakeHttpError(500, "internalError"),
        FakeHttpError(500, "internalError"),
        FakeHttpError(500, "internalError"),
    )
    client = ResilientYouTubeClient(keys=["k1"], max_retries=3, _service_factory=factory)

    with pytest.raises(CollectionExhausted):
        client.make_callables().list_videos(part="snippet")
```

`tests/test_youtube_client.py` 상단 import에 `CollectionExhausted` 추가:

```python
from autoresearch.youtube_collection.client import (
    CollectionExhausted,
    ResilientYouTubeClient,
    YouTubeCallables,
)
```

- [ ] **Step 2: 테스트 실행 — 실패 확인**

Run: `pytest tests/test_youtube_client.py -k "5xx" -v`
Expected: FAIL — 현재 정상 경로만 구현됨, 예외 전파 안 됨.

- [ ] **Step 3: tenacity 재시도 구현**

`client.py` import 섹션에 추가:

```python
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)
```

`_make_resilient_callable` 를 아래로 교체 (BACKOFF verdict 처리 + tenacity):

```python
    def _make_resilient_callable(self, resource: str) -> Callable[..., dict]:
        """자원별 복원력 callable. tenacity 안쪽 + 외곽 회전/터미널 루프."""

        def resilient(**kw) -> dict:
            return self._call_with_resilience(resource, kw)

        return resilient

    def _call_with_resilience(self, resource: str, kw: dict) -> dict:
        """외곽 루프: Key 회전 + 터미널 판정. tenacity는 안쪽."""
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
                # ROTATE/TERMINAL/IP_BAN_CANDIDATE 는 후속 태스크에서 처리.
                # Task 4에서는 BACKOFF만 다루므로, 나머지는 우선 terminal 로 승격(임시).
                raise CollectionExhausted(
                    f"verdict={verdict} resource={resource} status={e.status} reason={e.reason}"
                )

    def _retry_with_backoff(self, list_callable: Callable[..., dict], kw: dict) -> dict:
        """tenacity — BACKOFF 분류 에러만 재시도. 다른 verdict는 즉시 예외로 승격."""
        for attempt in Retrying(
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential_jitter(initial=1, max=30),
            retry=retry_if_exception_type(_RetryableHttpError),
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
```

그리고 모듈 레벨에 `_RetryableHttpError` + `_try_wrap_http_error` 추가 (`_classify_error` 위):

```python
class _RetryableHttpError(Exception):
    """내부용: googleapiclient/requests 예외를 정규화한 재시도 후보 에러.

    status/reason 을 가지며, _retry_with_backoff 가 tenacity 로 재시도하고
    _classify_error 가 verdict 로 분류한다.
    """

    def __init__(self, status: int, reason: str | None, original: Exception | None = None):
        self.status = status
        self.reason = reason
        self.original = original
        super().__init__(f"status={status} reason={reason}")


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
```

`import json` 을 `client.py` 상단에 추가.

- [ ] **Step 4: 테스트 실행 — 통과 확인**

Run: `pytest tests/test_youtube_client.py -k "5xx" -v`
Expected: 2개 PASS

- [ ] **Step 5: 정상 경로 회귀**

Run: `pytest tests/test_youtube_client.py -k "normal_path or make_callables or classify" -v`
Expected: 전체 PASS

- [ ] **Step 6: Commit**

```bash
git add autoresearch/youtube_collection/client.py tests/test_youtube_client.py
git commit -m "feat: tenacity 재시도 추가 (5xx/네트워크/rateLimit backoff 후 소진 시 CollectionExhausted)"
```

---

## Task 5: Key 회전 (keyInvalid/keyExpired/401 → 다음 Key)

**Files:**
- Modify: `autoresearch/youtube_collection/client.py`
- Test: `tests/test_youtube_client.py`

**Interfaces:**
- Consumes: Task 4 (`_call_with_resilience`)
- Produces: ROTATE verdict 시 해당 Key를 `_invalid_keys`에 마킹하고 다음 활성 Key로 회전. 모든 Key 소진 시 `CollectionExhausted`.

- [ ] **Step 1: 실패 테스트 작성**

```python
def test_key_invalid_rotates_to_next_key_and_succeeds():
    """k1 → 400 keyInvalid → k2 → 200. Key 무효화 마킹 + 회전 성공."""
    state = {"k1_calls": 0}

    def factory(key):
        def list_videos(**kw):
            if key == "k1":
                state["k1_calls"] += 1
                raise FakeHttpError(400, "keyInvalid")
            return _fake_videos_response()  # k2 정상
        return list_videos, lambda **kw: {"items": []}, lambda **kw: {"items": []}

    client = ResilientYouTubeClient(
        keys=["k1", "k2"], max_retries=2, _service_factory=factory
    )
    result = client.make_callables().list_videos(part="snippet")

    assert result == _fake_videos_response()
    assert state["k1_calls"] == 1  # 1회만 호출, tenacity 반복 X (ROTATE는 즉시)


def test_key_expired_treated_same_as_key_invalid():
    """400 keyExpired → 회전."""
    def factory(key):
        def list_videos(**kw):
            if key == "k1":
                raise FakeHttpError(400, "keyExpired")
            return _fake_videos_response()
        return list_videos, lambda **kw: {"items": []}, lambda **kw: {"items": []}

    client = ResilientYouTubeClient(keys=["k1", "k2"], _service_factory=factory)
    result = client.make_callables().list_videos(part="snippet")

    assert result == _fake_videos_response()


def test_401_auth_rotates_to_next_key():
    """401 unauthorized → 회전."""
    def factory(key):
        def list_videos(**kw):
            if key == "k1":
                raise FakeHttpError(401, "unauthorized")
            return _fake_videos_response()
        return list_videos, lambda **kw: {"items": []}, lambda **kw: {"items": []}

    client = ResilientYouTubeClient(keys=["k1", "k2"], _service_factory=factory)
    result = client.make_callables().list_videos(part="snippet")

    assert result == _fake_videos_response()


def test_all_keys_invalid_raises_collection_exhausted():
    """k1, k2 모두 keyInvalid → CollectionExhausted."""
    def factory(key):
        def list_videos(**kw):
            raise FakeHttpError(400, "keyInvalid")
        return list_videos, lambda **kw: {"items": []}, lambda **kw: {"items": []}

    client = ResilientYouTubeClient(keys=["k1", "k2"], _service_factory=factory)

    with pytest.raises(CollectionExhausted):
        client.make_callables().list_videos(part="snippet")
```

- [ ] **Step 2: 테스트 실행 — 실패 확인**

Run: `pytest tests/test_youtube_client.py -k "key_invalid or key_expired or 401_auth or all_keys" -v`
Expected: FAIL — 현재 ROTATE도 임시로 CollectionExhausted 처리.

- [ ] **Step 3: ROTATE 분기 구현**

`_call_with_resilience` 의 except 블록에서 ROTATE 처리를 추가 (임시 terminal 승격 부분 교체):

```python
            except _RetryableHttpError as e:
                verdict = _classify_error(e.status, e.reason)
                self._log_decision(resource, key, "normal", verdict, e)
                last_verdict = verdict
                if verdict is Verdict.BACKOFF:
                    raise CollectionExhausted(
                        f"일시 장애 backoff 소진 resource={resource} "
                        f"status={e.status} reason={e.reason}"
                    )
                if verdict is Verdict.ROTATE:
                    self._invalid_keys.add(key)
                    continue  # 다음 활성 Key 로 while 루프 재시도
                # TERMINAL_QUOTA/TERMINAL_CONFIG/IP_BAN_CANDIDATE — 후속 태스크.
                raise CollectionExhausted(
                    f"verdict={verdict} resource={resource} status={e.status} reason={e.reason}"
                )
```

- [ ] **Step 4: 테스트 실행 — 통과 확인**

Run: `pytest tests/test_youtube_client.py -k "key_invalid or key_expired or 401_auth or all_keys" -v`
Expected: 4개 PASS

- [ ] **Step 5: 회귀**

Run: `pytest tests/ -v`
Expected: 전체 PASS

- [ ] **Step 6: Commit**

```bash
git add autoresearch/youtube_collection/client.py tests/test_youtube_client.py
git commit -m "feat: Key 롤링 추가 (keyInvalid/keyExpired/401 → 다음 Key 회전, 전부 소진 시 CollectionExhausted)"
```

---

## Task 6: 프로젝트 단위 실패 즉시 skip (quotaExceeded/accessNotConfigured)

**Files:**
- Modify: `autoresearch/youtube_collection/client.py`
- Test: `tests/test_youtube_client.py`

**Interfaces:**
- Consumes: Task 5
- Produces: TERMINAL_QUOTA/TERMINAL_CONFIG verdict 시 회전 없이 즉시 `CollectionExhausted` (skip+알림으로 승격).

- [ ] **Step 1: 실패 테스트 작성**

```python
def test_quota_exceeded_skips_without_rotation():
    """403 quotaExceeded → 회전 없이 즉시 CollectionExhausted(프로젝트 단위)."""
    call_count = {"n": 0}

    def factory(key):
        def list_videos(**kw):
            call_count["n"] += 1
            raise FakeHttpError(403, "quotaExceeded")
        return list_videos, lambda **kw: {"items": []}, lambda **kw: {"items": []}

    client = ResilientYouTubeClient(keys=["k1", "k2"], _service_factory=factory)

    with pytest.raises(CollectionExhausted):
        client.make_callables().list_videos(part="snippet")
    # 회전 안 했음 — k1 1회 호출 후 즉시 터미널.
    assert call_count["n"] == 1


def test_access_not_configured_skips_without_rotation():
    """403 accessNotConfigured → 회전 없이 즉시 CollectionExhausted(프로젝트 스코프)."""
    call_count = {"n": 0}

    def factory(key):
        def list_videos(**kw):
            call_count["n"] += 1
            raise FakeHttpError(403, "accessNotConfigured")
        return list_videos, lambda **kw: {"items": []}, lambda **kw: {"items": []}

    client = ResilientYouTubeClient(keys=["k1", "k2"], _service_factory=factory)

    with pytest.raises(CollectionExhausted):
        client.make_callables().list_videos(part="snippet")
    assert call_count["n"] == 1  # k2 로 회전 안 함
```

- [ ] **Step 2: 테스트 실행 — 통과 확인**

Run: `pytest tests/test_youtube_client.py -k "quota_exceeded or access_not_configured" -v`
Expected: 이미 통과(Task 5의 임시 terminal 승격이 QUOTA/CONFIG 도 잡음). 통과 확인 후 Step 3에서 명시화.

- [ ] **Step 3: TERMINAL_QUOTA/TERMINAL_CONFIG 명시적 분기**

`_call_with_resilience` 의 except 블록에서 TERMINAL 명시 (임시 catch-all 교체):

```python
                if verdict is Verdict.TERMINAL_QUOTA:
                    raise CollectionExhausted(
                        f"프로젝트 일일 쿼터 소진 — 회전 무효 resource={resource} reason={e.reason}"
                    )
                if verdict is Verdict.TERMINAL_CONFIG:
                    raise CollectionExhausted(
                        f"프로젝트 설정 문제(accessNotConfigured) — 회전 무효 "
                        f"resource={resource}"
                    )
                if verdict is Verdict.IP_BAN_CANDIDATE:
                    # 후속 태스크(시그니처 누적).
                    self._record_ip_ban_candidate(key, resource, e.reason)
                    continue  # 임시: 다음 Key (Task 7 에서 시그니처 판정으로 교체)
                raise CollectionExhausted(
                    f"verdict={verdict} resource={resource} status={e.status} reason={e.reason}"
                )
```

그리고 `_record_ip_ban_candidate` 스텁 + 상태 초기화:

`__init__` 에 추가:
```python
        self._ip_ban_candidates: dict[str, str] = {}  # key → reason (per resource 라운드)
```

메서드 추가:
```python
    def _record_ip_ban_candidate(self, key: str, resource: str, reason: str | None) -> None:
        """Task 7 에서 시그니처 판정에 사용. Task 6 은 기록만."""
        self._ip_ban_candidates[key] = reason or ""
```

- [ ] **Step 4: 테스트 실행 — 통과 확인**

Run: `pytest tests/test_youtube_client.py -k "quota_exceeded or access_not_configured" -v`
Expected: 2개 PASS

- [ ] **Step 5: 회귀**

Run: `pytest tests/ -v`
Expected: 전체 PASS

- [ ] **Step 6: Commit**

```bash
git add autoresearch/youtube_collection/client.py tests/test_youtube_client.py
git commit -m "feat: 프로젝트 단위 실패(quota/accessNotConfigured) 즉시 skip — 회전 무효 명시"
```

---

## Task 7: IP밴 시그니처 + `proxy_url=None` 숏트서킷

**Files:**
- Modify: `autoresearch/youtube_collection/client.py`
- Test: `tests/test_youtube_client.py`

**Interfaces:**
- Consumes: Task 6 (`_record_ip_ban_candidate`)
- Produces: IP밴 시그니처 판정(엣지 규칙: 최소 Key≥2, 동일 reason, 부분 성공 불성립, reason 파싱 실패 미카운트, per-run). `proxy_url=None`이면 시그니처 감지 시 즉시 `CollectionExhausted`(숏트서킷).

**엣지 규칙(설계 §5.2):**
- 활성 Key ≥2 일 때만 시그니처 인정.
- 모든 활성 Key가 IP_BAN_CANDIDATE(403 계열)로 동일 실패.
- 일부만 실패/성공(부분 성공) → 시그니처 불성립.
- reason 파싱 실패(reason=None) → 시그니처 미카운트(이미 BACKOFF로 분류되므로 IP_BAN_CANDIDATE에 안 옴).

- [ ] **Step 1: 실패 테스트 작성**

```python
def test_ip_ban_signature_proxy_none_short_circuits():
    """전 Key 동일 403(기타 reason) + proxy_url=None → 즉시 CollectionExhausted.

    시그니처 성립(Key≥2, 전 Key 동일 403 IP_BAN_CANDIDATE).
    """
    def factory(key):
        def list_videos(**kw):
            raise FakeHttpError(403, "suspended")  # 기타 403 → IP_BAN_CANDIDATE
        return list_videos, lambda **kw: {"items": []}, lambda **kw: {"items": []}

    client = ResilientYouTubeClient(
        keys=["k1", "k2"], proxy_url=None, _service_factory=factory
    )

    with pytest.raises(CollectionExhausted, match="IP 밴"):
        client.make_callables().list_videos(part="snippet")


def test_ip_ban_signature_single_key_does_not_qualify():
    """Key 1개 + 403 → 시그니처 미성립(최소 Key≥2). CollectionExhausted(rotate 소진)."""
    def factory(key):
        def list_videos(**kw):
            raise FakeHttpError(403, "suspended")
        return list_videos, lambda **kw: {"items": []}, lambda **kw: {"items": []}

    client = ResilientYouTubeClient(
        keys=["k1"], proxy_url=None, _service_factory=factory
    )

    with pytest.raises(CollectionExhausted) as exc_info:
        client.make_callables().list_videos(part="snippet")
    # IP 밴 메시지가 아님 — 시그니처 미성립으로 회전 소진 처리.
    assert "IP 밴" not in str(exc_info.value)


def test_ip_ban_signature_partial_success_does_not_qualify():
    """k1=403 suspended, k2=200 → 부분 성공, 시그니처 불성립 → k2 응답 반환."""
    def factory(key):
        def list_videos(**kw):
            if key == "k1":
                raise FakeHttpError(403, "suspended")
            return _fake_videos_response()
        return list_videos, lambda **kw: {"items": []}, lambda **kw: {"items": []}

    client = ResilientYouTubeClient(
        keys=["k1", "k2"], proxy_url=None, _service_factory=factory
    )
    result = client.make_callables().list_videos(part="snippet")

    assert result == _fake_videos_response()


def test_ip_ban_signature_uses_proxy_when_configured():
    """전 Key 동일 403 + proxy_url 있음 → 프록시 경로로 전환 (1차: stub).

    1차 PR은 proxy_url 이 있어도 실제 프록시 호출은 stub(프록시 미배포).
    시그니처 감지 → 프록시 시도 → 여기선 프록시도 동일 에러 → CollectionExhausted.
    """
    def factory(key):
        def list_videos(**kw):
            raise FakeHttpError(403, "suspended")
        return list_videos, lambda **kw: {"items": []}, lambda **kw: {"items": []}

    # proxy_url 주입했지만 _proxy_callable 주입 안 했으므로 기본(동일 factory 재사용).
    client = ResilientYouTubeClient(
        keys=["k1", "k2"],
        proxy_url="https://fake-proxy.example.com",
        _service_factory=factory,
    )

    with pytest.raises(CollectionExhausted):
        client.make_callables().list_videos(part="snippet")
```

- [ ] **Step 2: 테스트 실행 — 실패/통과 혼합 확인**

Run: `pytest tests/test_youtube_client.py -k "ip_ban" -v`
Expected: 일부 FAIL (현재 Task 6 stub은 단순 continue 라 시그니처 판정 없음).

- [ ] **Step 3: 시그니처 판정 + 숏트서킷 구현**

`__init__` 에 Circuit Breaker 상태 추가:

```python
        # Circuit Breaker (per-run). CLOSED/OPEN 이항(매일 새 인스턴스로 리셋).
        self._breaker_open: bool = False
        # IP밴 시그니처 — 현재 자원 호출 라운드에서 IP_BAN_CANDIDATE 누적.
        self._ip_ban_candidates: dict[str, str] = {}  # key → reason
        self._proxy_attempts: int = 0
```

`_record_ip_ban_candidate`를 판정 로직으로 교체:

```python
    def _record_ip_ban_candidate(self, key: str, resource: str, reason: str | None) -> None | Verdict:
        """IP밴 시그니처 판정. None=계속 회전, Verdict=터미널 판정."""
        if reason is None:
            # reason 파싱 실패 → 시그니처 미카운트(엣지 규칙). 회전 계속.
            return None
        self._ip_ban_candidates[key] = reason
        active_keys = [k for k in self._keys if k not in self._invalid_keys]
        if len(active_keys) < 2:
            # 최소 Key≥2 아니면 시그니처 불성립. 회전 계속(결국 소진).
            return None
        # 모든 활성 Key 가 이미 candidate 인가?(부분 성공이면 일부만 candidate)
        if all(k in self._ip_ban_candidates for k in active_keys):
            # 동일 reason 인가?(엣지: reason 정확히 같아야)
            reasons = {self._ip_ban_candidates[k] for k in active_keys}
            if len(reasons) == 1:
                # 시그니처 성립.
                return Verdict.IP_BAN_CANDIDATE  # 호출자가 터미널 처리
        return None
```

`_call_with_resilience` 의 IP_BAN_CANDIDATE 분기를 교체:

```python
                if verdict is Verdict.IP_BAN_CANDIDATE:
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
                    self._invalid_keys.add(key)  # 임시: 회전 촉진
                    continue
```

`_call_via_proxy` 스텁 추가 (1차 PR은 프록시 미배포, 호출 자체가 에러):

```python
    def _call_via_proxy(self, resource: str, kw: dict) -> dict:
        """프록시 경로 호출. 1차 PR 은 프록시 미배포 → 시그니처 후 즉시 CollectionExhausted.

        2차 PR 에서 requests.get(proxy_url/youtube/v3/...) 로 구현.
        """
        raise CollectionExhausted(
            f"프록시 경로 미구현(1차 PR) resource={resource} proxy_url={self._proxy_url}"
        )
```

`_call_with_resilience` 시작에 Breaker 체크 추가:

```python
    def _call_with_resilience(self, resource: str, kw: dict) -> dict:
        if self._breaker_open:
            raise CollectionExhausted(
                f"Circuit Breaker OPEN resource={resource} (이전 호출에서 폭주 확정)"
            )
        # ... (기존 로직)
```

- [ ] **Step 4: 테스트 실행 — 통과 확인**

Run: `pytest tests/test_youtube_client.py -k "ip_ban" -v`
Expected: 4개 PASS

- [ ] **Step 5: 회귀**

Run: `pytest tests/ -v`
Expected: 전체 PASS

- [ ] **Step 6: Commit**

```bash
git add autoresearch/youtube_collection/client.py tests/test_youtube_client.py
git commit -m "feat: IP밴 시그니처 판정 + proxy_url=None 숏트서킷 + Circuit Breaker 상태"
```

---

## Task 8: `max_total_calls` 폭주 가드 + 관측성 로깅 강화

**Files:**
- Modify: `autoresearch/youtube_collection/client.py`
- Test: `tests/test_youtube_client.py`

**Interfaces:**
- Consumes: Task 7
- Produces: `_check_call_budget`가 `max_total_calls` 초과 시 `CollectionExhausted`. 관측성 로그가 모든 의사결정 지점에서 key_index/route/verdict/attempt/action을 기록(Key/헤더/본문 마스킹).

- [ ] **Step 1: 실패 테스트 작성**

```python
def test_max_total_calls_guards_against_runaway():
    """max_total_calls=3 초과 → CollectionExhausted (무효 Key 반복 루프 방지)."""
    def factory(key):
        def list_videos(**kw):
            return _fake_videos_response()  # 정상이어도 호출 수 누적
        return list_videos, lambda **kw: {"items": []}, lambda **kw: {"items": []}

    client = ResilientYouTubeClient(
        keys=["k1"], max_total_calls=3, _service_factory=factory
    )
    callables = client.make_callables()
    callables.list_videos(part="a")
    callables.list_channels(part="b")
    callables.list_categories(part="c")

    with pytest.raises(CollectionExhausted, match="폭주 가드"):
        callables.list_videos(part="d")  # 4회째
```

- [ ] **Step 2: 테스트 실행 — 실패 확인**

Run: `pytest tests/test_youtube_client.py -k "max_total_calls" -v`
Expected: FAIL (현재 `_check_call_budget` 스텁).

- [ ] **Step 3: 폭주 가드 구현**

`_check_call_budget` 채우기:

```python
    def _check_call_budget(self) -> None:
        if self._call_count >= self._max_total_calls:
            raise CollectionExhausted(
                f"폭주 가드: max_total_calls={self._max_total_calls} 도달 "
                f"(call_count={self._call_count})"
            )
```

- [ ] **Step 4: 테스트 실행 — 통과 확인**

Run: `pytest tests/test_youtube_client.py -k "max_total_calls" -v`
Expected: PASS

- [ ] **Step 5: 관측성 — _log_decision이 모든 분기에서 호출됨 확인 (수동 리뷰)**

`_call_with_resilience` 의 BACKOFF/ROTATE/TERMINAL/IP_BAN_CANDIDATE 분기 전부 `self._log_decision(...)` 호출 포함 확인. 없으면 추가. (구현에는 이미 있음 — Task 4에서 추가.)

- [ ] **Step 6: 회귀 + 전체 시나리오 실행**

Run: `pytest tests/ -v`
Expected: 전체 PASS (16개 시나리오 중 구현된 것 전부)

- [ ] **Step 7: Commit**

```bash
git add autoresearch/youtube_collection/client.py tests/test_youtube_client.py
git commit -m "feat: max_total_calls 폭주 가드 + 관측성 로깅(key_index만, 값/헤더/본문 마스킹)"
```

---

## Task 9: DAG 교체 (`AirflowFailException` 승격)

**Files:**
- Modify: `dags/youtube_trending_kr_daily.py`
- Test: 수동 검증(DAG 파일 import + 정적 확인). 단위테스트는 client.py에서 이미 검증됨.

**Interfaces:**
- Consumes: `ResilientYouTubeClient`, `CollectionExhausted` (client.py)
- Produces: DAG `_build_service`/`_make_callables` 제거 → `ResilientYouTubeClient.make_callables()`. `CollectionExhausted` → `AirflowFailException` 승격.

- [ ] **Step 1: DAG 수정 — client 사용 + AirflowFailException 승격**

`dags/youtube_trending_kr_daily.py` 수정:

import 섹션 — `_build_service`/`_make_callables` 제거, client import 추가:

```python
from airflow.decorators import dag, task
from airflow.exceptions import AirflowFailException
from airflow.models import Variable

from autoresearch.youtube_collection.client import (
    CollectionExhausted,
    ResilientYouTubeClient,
    YouTubeCallables,
)
from autoresearch.youtube_collection.fetch import collect_trending
from autoresearch.youtube_collection.load import write_partition
```

`_build_service`, `_make_callables` 함수 제거. 대신 `_load_keys`, `_build_client` 추가:

```python
def _load_keys() -> list[str]:
    """API Key 풀 로드: YOUTUBE_API_KEYS(복수, 쉼표) 우선, 없으면 단수 폴백."""
    raw = _get_config("YOUTUBE_API_KEYS")
    if raw:
        keys = [k.strip() for k in raw.split(",") if k.strip()]
        if keys:
            return keys
    single = _get_config("YOUTUBE_API_KEY")
    if single:
        return [single]
    raise RuntimeError(
        "YOUTUBE_API_KEYS(또는 YOUTUBE_API_KEY) 가 설정되지 않음"
    )


def _build_client() -> ResilientYouTubeClient:
    """ResilientYouTubeClient 생성. proxy_url 은 환경변수(1차: 보통 None)."""
    return ResilientYouTubeClient(
        keys=_load_keys(),
        proxy_url=_get_config("YOUTUBE_PROXY_URL"),
    )
```

`snapshot` 태스크 본문 — `collect_trending` 호출을 try/except 로 감싸 승격:

```python
    @task
    def snapshot() -> str:
        client = _build_client()
        callables: YouTubeCallables = client.make_callables()
        collected_at = datetime.now(UTC)
        partition_date = collected_at.astimezone(_KST).date()

        try:
            videos = collect_trending(
                callables.list_videos,
                callables.list_channels,
                callables.list_categories,
                collected_at=collected_at,
                region_code="KR",
                max_results=DEFAULT_MAX_RESULTS,
            )
        except CollectionExhausted as e:
            # 터미널: 모든 Key·경로 소진. 재시도 무의미 → 즉시 failed 승격.
            raise AirflowFailException(
                f"유튜브 수집 폭주 — 그날 partition skip: {e}"
            ) from e

        bucket = _get_config("YOUTUBE_LAKE_BUCKET")
        if not bucket:
            raise RuntimeError(
                "YOUTUBE_LAKE_BUCKET is not set (env or Airflow Variable)"
            )
        base_path = f"{bucket}/{LAKE_DIR_NAME}"
        path = write_partition(
            videos, base_path, partition_date, filesystem=_gcs_filesystem()
        )
        logger.info("Daily snapshot complete: %d videos -> %s", len(videos), path)
        return path
```

DAG 헤더 docstring의 "설정" 섹션도 업데이트(`YOUTUBE_API_KEYS` 복수 명시):

```python
"""[일일 수집 DAG] 한국(KR) 유튜브 트렌딩 → GCS Data Lake.

매일 그날의 KR 트렌딩 영상(약 200개)을 수집해 ``dt=YYYY-MM-DD`` hive 파티션으로
GCS 레이크에 append 한다. append-only 일별 스냅샷 모델의 실시간 축.

스케줄: ``30 15 * * *`` (UTC 15:30 == KST 00:30)
    → 그날 트렌딩이 어느 정도 확정된 새벽에 찍는다. dt 키는 KST 수집일.

필요 설정(환경변수 또는 Airflow Variable):
  * ``YOUTUBE_API_KEYS``  - YouTube Data API v3 키(쉼표 구분 복수, Key 무효화 대응)
  * ``YOUTUBE_LAKE_BUCKET`` - GCS 버킷명(gs:// 접두사 없음)
  * ``YOUTUBE_PROXY_URL``  - (선택) Cloud Run 프록시 URL. 1차: 미설정=비활성.

인증:
  * GCP: Application Default Credentials(로컬은 ``gcloud auth application-default
    login``, prod K8s 은 Workload Identity — 인프라 담당).
  * YouTube: API 키 풀만으로 충분(쿼터 ~7 units/일, 예산 10,000 대비 0.07%).

복원력:
  * ResilientYouTubeClient 가 재시도(tenacity) + Key 롤링 + IP밴 시그니처 +
    Circuit Breaker + skip 을 담당. CollectionExhausted → AirflowFailException
    승격으로 터미널 실패(retries 무관 즉시 failed).

이 파일은 '얇은 래퍼'다. 실제 로직은 autoresearch.youtube_collection(순수 Python,
단위테스트 가능)에 있고, 여기선 Airflow TaskFlow 로 그것들을 엮기만 한다.
"""
```

- [ ] **Step 2: DAG 파일 import 검증**

Run: `python -c "import ast; ast.parse(open('dags/youtube_trending_kr_daily.py').read()); print('syntax OK')"`
Expected: `syntax OK`

- [ ] **Step 3: 기존 fetch.py 테스트 회귀 (DAG 변경이 fetch.py 에 영향 없음 확인)**

Run: `pytest tests/test_youtube_collection_fetch.py -v`
Expected: 전체 PASS

- [ ] **Step 4: 전체 회귀**

Run: `pytest tests/ -v`
Expected: 전체 PASS

- [ ] **Step 5: Commit**

```bash
git add dags/youtube_trending_kr_daily.py
git commit -m "feat: 일일 DAG 를 ResilientYouTubeClient 로 교체 + CollectionExhausted → AirflowFailException 승격"
```

---

## Task 10: CI 보강 (pip-audit/gitleaks) — 권장

**Files:**
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Consumes: 없음
- Produces: CI에 의존성 감사(`pip-audit`) + 시크릿 스캔(`gitleaks`) 스텝. security 에이전트 M1 권고 반영.

- [ ] **Step 1: 현재 ci.yml 확인**

Run: `cat .github/workflows/ci.yml`
(이미 pytest 3.11/3.12 + Docker build 포함 확인됨.)

- [ ] **Step 2: pip-audit + gitleaks 스텝 추가**

기존 워크플로우에 아래 잡/스텝 추가(세부 YAML 은 기존 구조에 맞춰 편집):

```yaml
  security:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0  # gitleaks 전체 히스토리 스캔
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install dependencies
        run: pip install -r requirements.txt -r requirements-dev.txt pip-audit
      - name: pip-audit (의존성 취약점 스캔)
        run: pip-audit -r requirements.txt
      - name: gitleaks (시크릿 스캔)
        uses: gitleaks/gitleaks-action@v2
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

- [ ] **Step 3: 로컬 pip-audit 사전 확인**

Run: `pip install pip-audit && pip-audit -r requirements.txt`
Expected: 알려진 취약점 없음(또는 출력 확인).

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: 의존성 감사(pip-audit) + 시크릿 스캔(gitleaks) 스텝 추가"
```

---

## Self-Review 체크리스트 (작성자가 plan 저장 후 실행)

**1. Spec coverage (설계 §8 시나리오 16개):**
- [x] 403 quotaExceeded → Task 6
- [x] 403 userRateLimitExceeded → Task 2 (BACKOFF) + Task 4 (backoff)
- [x] 403 keyInvalid → 다음 Key 200 → Task 5
- [x] 403 accessNotConfigured → Task 6
- [x] 400 keyExpired → Task 5
- [x] 5xx backoff 후 성공 → Task 4
- [x] 5xx backoff 전부 소진 → Task 4
- [x] DNS gaierror/SSLError → Task 4 (`_try_wrap_http_error` net_types)
- [x] reason 파싱 실패(malformed) → Task 2 (`_classify_error` 빈 reason → BACKOFF)
- [x] IP밴 시그니처 + proxy_url 있음 → Task 7
- [x] IP밴 시그니처 + proxy_url=None → Task 7
- [x] 활성 Key 1개 + 403 → Task 7 (시그니처 미성립)
- [x] 부분 성공 → Task 7
- [x] 중간 배치 전환 → per-run 상태 + Circuit Breaker OPEN 후속 호출 skip 로 설계 커버(Task 7 breaker_open 체크). 별도 테스트는 breaker_open 경유 케이스로 간접 검증.
- [x] CollectionExhausted → AirflowFailException 승격 → Task 9
- [x] max_total_calls 폭주 가드 → Task 8

**2. Placeholder scan:** TBD/TODO/구현 나중에 없는지. `_call_via_proxy`는 "1차 PR은 stub"으로 명시(2차 PR 범위 명시적 non-goal). OK.

**3. Type consistency:** `Verdict` enum 값 이름 전 태스크 일관. `YouTubeCallables` 필드명(list_videos/list_channels/list_categories) 전 태스크 일관. `_classify_error(status, reason)` 시그니처 일관.

**4. 마스킹 정책:** `_log_decision`이 `key_index`만 쓰고 Key 값/헤더/본문 안 씀. 테스트도 Key 값을 assert 하지 않음(index 만). OK.

---

## Execution Handoff

Plan complete. 이제 실행 옵션:

**1. Subagent-Driven (recommended)** — 태스크별 fresh 서브에이전트 dispatch, 태스크 간 리뷰. 빠른 반복.

**2. Inline Execution** — 이 세션에서 executing-plans 스킬로 배치 실행, 체크포인트마다 리뷰.

어느 쪽으로 진행할까요?
