"""rerank API 노출 순위 클라이언트.

전체 파이프라인 기준으로 이 모듈은 "노출 순위의 출처" 구간만 담당한다 —
Inference Server(FastAPI `/rerank`)를 유저 단위로 호출해 응답 점수를
`RankedVideo` 순위로 변환하고, 그 순위를 기존 노출 조립기에 넘길 lazy
provider를 만든다. 노출 24개 조립·태그 규칙은 `model_exposure_provider`가,
LLM 클릭 판정·저장은 `autoresearch.action_logs`가 소유하며 여기서 담당하지
않는다. 서버 자체(피처 조회·모델 추론)는 `src/serving`이 소유한다.

제공 기능:

- `select_candidate_video_ids` — RerankRequest 상한(200)에 맞는 결정적 후보 선택
- `rank_user` — 유저 1명 `/rerank` 호출 → (`RankedVideo` 목록, model_id)
- `make_rerank_api_exposure_provider` — CandidateProvider seam에 주입할
  lazy HTTP provider (호출 실패·model_id 혼합은 fail-fast, 휴리스틱 대체 금지)

spec: docs/specs/2026-07-23-rerank-api-exposure-source.md
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Sequence

import requests

from src.pipeline.model_exposure_provider import (
    ModelExposureRound,
    RankedVideo,
    build_model_exposures,
)

logger = logging.getLogger(__name__)

# RerankRequest.video_ids 상한 (src/serving/schemas.py 계약과 동일)
MAX_CANDIDATES_PER_REQUEST = 200

# 재시도 백오프 기저(초). 시도 n 실패 후 _BACKOFF_BASE_SEC * n 만큼 대기한다.
_BACKOFF_BASE_SEC = 0.5


class RerankApiError(RuntimeError):
    """rerank API 호출 실패(재시도 소진·4xx·응답 계약 위반·계보 혼합)."""


@dataclass(frozen=True, slots=True)
class RerankApiSettings:
    """rerank API 접속 설정."""

    base_url: str
    timeout_sec: float = 30.0
    max_attempts: int = 3


def select_candidate_video_ids(
    videos: Sequence[dict], limit: int = MAX_CANDIDATES_PER_REQUEST
) -> list[str]:
    """RerankRequest 상한에 맞춰 결정적으로 후보 video_id를 고른다.

    view_count 내림차순, 동률은 video_id 오름차순 — `build_model_exposures`의
    popular_pool 정렬과 동일 기준이라 같은 pool이면 항상 같은 후보가 나간다.
    중복 video_id는 첫 항목만 남긴다(RerankRequest가 중복을 거부한다).
    """
    unique: dict[str, dict] = {}
    for video in videos:
        video_id = str(video.get("video_id", ""))
        if video_id and video_id not in unique:
            unique[video_id] = video
    ordered = sorted(
        unique.values(),
        key=lambda v: (-int(v.get("view_count", 0) or 0), str(v["video_id"])),
    )
    return [str(v["video_id"]) for v in ordered[:limit]]


def _parse_response_items(user_id: str, payload: object) -> tuple[list[RankedVideo], str]:
    """/rerank 응답 본문을 검증하고 순위·계보로 변환한다."""
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise RerankApiError(f"rerank 응답 본문이 계약과 다릅니다 (user_id={user_id})")
    items = payload["items"]
    if not items:
        raise RerankApiError(f"rerank 응답 items가 비어 있습니다 (user_id={user_id})")

    model_ids: set[str] = set()
    scored: list[tuple[float, str]] = []
    for item in items:
        video_id = str(item.get("video_id", ""))
        ctr_score = item.get("ctr_score")
        model_id = str(item.get("model_id", ""))
        if not video_id or not isinstance(ctr_score, (int, float)) or not model_id:
            raise RerankApiError(
                f"rerank 응답 항목이 계약과 다릅니다 (user_id={user_id})"
            )
        scored.append((float(ctr_score), video_id))
        model_ids.add(model_id)
    if len(model_ids) != 1:
        raise RerankApiError(
            f"rerank 응답 한 건에 model_id가 혼재합니다 (user_id={user_id}): "
            f"{sorted(model_ids)}"
        )

    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    ranking = [
        RankedVideo(video_id=video_id, rank=position, ctr_score=score)
        for position, (score, video_id) in enumerate(scored, start=1)
    ]
    return ranking, model_ids.pop()


def rank_user(
    settings: RerankApiSettings,
    user_id: str,
    video_ids: Sequence[str],
    *,
    session: requests.Session,
) -> tuple[list[RankedVideo], str]:
    """유저 1명을 `/rerank`로 순위화한다. (순위 목록, model_id)를 반환한다.

    연결 오류와 5xx만 `max_attempts`까지 재시도한다. 4xx는 요청 자체의 결함
    (계약 위반)이므로 즉시 실패한다. 응답 items는 ctr_score 내림차순(동률은
    video_id 오름차순)으로 정렬해 rank 1..N을 부여한다.
    """
    url = settings.base_url.rstrip("/") + "/rerank"
    body = {"user_id": user_id, "video_ids": list(video_ids)}
    last_error: Exception | None = None
    for attempt in range(1, settings.max_attempts + 1):
        try:
            response = session.post(url, json=body, timeout=settings.timeout_sec)
        except requests.RequestException as error:
            last_error = error
            logger.warning(
                "rerank request failed (attempt %d/%d, user_id=%s): %s",
                attempt,
                settings.max_attempts,
                user_id,
                type(error).__name__,
            )
        else:
            if response.status_code >= 500:
                last_error = RerankApiError(
                    f"rerank 서버 오류 status={response.status_code} (user_id={user_id})"
                )
                logger.warning(
                    "rerank server error (attempt %d/%d, user_id=%s): status=%d",
                    attempt,
                    settings.max_attempts,
                    user_id,
                    response.status_code,
                )
            elif response.status_code >= 400:
                # 요청 결함(중복 video_id, 상한 초과 등)은 재시도해도 같다.
                raise RerankApiError(
                    f"rerank 요청이 거부되었습니다 status={response.status_code} "
                    f"(user_id={user_id})"
                )
            else:
                return _parse_response_items(user_id, response.json())
        if attempt < settings.max_attempts:
            time.sleep(_BACKOFF_BASE_SEC * attempt)
    raise RerankApiError(
        f"rerank 호출이 {settings.max_attempts}회 모두 실패했습니다 "
        f"(user_id={user_id}): {type(last_error).__name__}"
    ) from last_error


def make_rerank_api_exposure_provider(
    settings: RerankApiSettings,
    videos: Sequence[dict],
    *,
    candidates_per_user: int = 24,
    personalized_ratio: float = 0.7,
    popular_ratio: float = 0.2,
    exploration_ratio: float = 0.1,
    session: requests.Session | None = None,
) -> ModelExposureRound:
    """CandidateProvider seam에 주입할 lazy rerank API provider를 만든다.

    provider가 유저 단위로 호출될 때마다 `/rerank`를 1회 부르고, 응답 순위를
    `build_model_exposures`에 넘겨 BQ 소스와 동일한 조립·태그를 얻는다.
    후보 pool은 생성 시 1회 고정한다. 한 라운드 안에서 응답 model_id가
    달라지면(서버가 도중에 재배포됨) 순위 계보가 섞이므로 즉시 실패한다.

    직렬성 전제: 파이프라인은 provider를 작업 목록 구축 단계(단일 스레드,
    `_generate_drafts_isolated` 1단계)에서만 호출하며, `--max-concurrency`는
    그 뒤의 LLM 콜(2단계 ThreadPoolExecutor)만 병렬화한다. `model_run_id`
    비교와 `metadata` 누적이 락 없이 안전한 근거가 이 직렬성이다 — 기존 BQ
    소스(`make_model_exposure_provider`)의 공유 맵도 같은 전제를 쓴다.
    provider 호출을 병렬화하려면 두 provider 모두 재설계가 필요하다.

    세션 소유권: `session`을 주입하면 호출자 소유이므로 여기서 닫지 않는다.
    주입이 없으면 내부에서 생성하며, CandidateProvider seam에는 라운드 종료
    훅이 없어 명시적으로 닫지 못한다 — 배치 CLI(프로세스 종료 시 소켓 회수)
    전제의 의도적 선택이다. 장수명 프로세스에서 재사용하려면 세션을 주입하고
    호출자가 수명을 관리해야 한다.
    """
    if not videos:
        raise RerankApiError("트렌딩 영상 pool 없이 노출을 조립할 수 없습니다")
    http = session if session is not None else requests.Session()
    candidate_ids = select_candidate_video_ids(videos)
    round_ = ModelExposureRound(provider=lambda vu, rng: [], metadata={}, model_run_id=None)

    def provider(virtual_user: dict, user_rng: random.Random) -> list[dict]:
        user_id = str(virtual_user.get("user_id", ""))
        ranking, model_id = rank_user(settings, user_id, candidate_ids, session=http)
        if round_.model_run_id is None:
            round_.model_run_id = model_id
        elif round_.model_run_id != model_id:
            raise RerankApiError(
                "라운드 도중 서버 model_id가 바뀌었습니다: "
                f"{round_.model_run_id} → {model_id} (user_id={user_id})"
            )
        candidates, user_meta = build_model_exposures(
            user_id,
            ranking,
            videos,
            user_rng,
            model_run_id=model_id,
            candidates_per_user=candidates_per_user,
            personalized_ratio=personalized_ratio,
            popular_ratio=popular_ratio,
            exploration_ratio=exploration_ratio,
        )
        round_.metadata.update(user_meta)
        return candidates

    round_.provider = provider
    return round_
