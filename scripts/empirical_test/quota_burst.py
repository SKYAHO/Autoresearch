"""Cloud Run proxy 경유 YouTube API 폭주 요청 스크립트.

목적: Cloud Run egress IP 밴 유도. 직접(로컬) 호출이 아닌 proxy 경유로
호출하여 Cloud Run 의 egress IP 가 제재 대상이 되도록 한다.

환경 변수:
    QUOTA_BURST_PROXY_URL: proxy 서비스 URL
    YOUTUBE_API_KEY: YouTube Data API v3 Key
    QUOTA_BURST_MAX_CALLS: 최대 호출 수 (기본 10000 = 일일 quota 상한)
    QUOTA_BURST_WORKERS: 병렬 요청 스레드 수 (기본 50)
"""
import os
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

PROXY_URL = os.environ["QUOTA_BURST_PROXY_URL"]
API_KEY = os.environ["YOUTUBE_API_KEY"]
MAX_CALLS = int(os.environ.get("QUOTA_BURST_MAX_CALLS", "10000"))
WORKERS = int(os.environ.get("QUOTA_BURST_WORKERS", "50"))
VIDEO_ID = "jNQXAC9IVRw"
ENDPOINT = f"{PROXY_URL.rstrip('/')}/youtube/v3/videos"


def _call_once(token: str) -> tuple[str, int, str]:
    """단일 호출. (kind, status_code, reason) 반환.

    kind: 'ok' | 'error' | 'network_error'
    status_code: HTTP 상태 코드 (network_error 시 -1)
    reason: 오류 reason (ok 시 빈 문자열)
    """
    try:
        resp = requests.get(
            ENDPOINT,
            params={"part": "snippet", "id": VIDEO_ID},
            headers={
                "X-Goog-Api-Key": API_KEY,
                "Authorization": f"bearer {token}",
            },
            timeout=30,
        )
    except requests.exceptions.RequestException as e:
        return ("network_error", -1, type(e).__name__)
    if resp.status_code == 200:
        return ("ok", 200, "")
    try:
        reason = resp.json().get("error", {}).get("errors", [{}])[0].get("reason", "?")
    except ValueError:
        reason = "(non-JSON)"
    return ("error", resp.status_code, reason)


def main() -> int:
    stats: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    token = __import__("subprocess").check_output(
        ["gcloud", "auth", "print-identity-token"]
    ).decode().strip()
    completed = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(_call_once, token) for _ in range(MAX_CALLS)}
        for fut in as_completed(futures):
            completed += 1
            kind, code, reason = fut.result()
            if kind == "network_error":
                stats["network_error"] += 1
            elif kind == "ok":
                stats[200] += 1
            else:
                stats[code] += 1
                reasons[reason] += 1
            if completed % 100 == 0:
                print(
                    f"[{completed}/{MAX_CALLS}] stats={dict(stats)} reasons={dict(reasons)}",
                    flush=True,
                )
            # IP밴 시그니처 의심: quotaExceeded가 아닌 403이 50건 이상이면 조기 종료.
            # quota 소진(quotaExceeded)은 IP밴이 아니므로 종료 트리거에서 제외한다.
            non_quota_403 = sum(
                count for r, count in reasons.items() if r != "quotaExceeded"
            )
            if non_quota_403 >= 50:
                print(
                    f"IP밴 시그니처 의심 — quotaExceeded 아닌 403 {non_quota_403}건, 조기 종료",
                    flush=True,
                )
                for f in futures:
                    f.cancel()
                break
    print(f"FINAL stats={dict(stats)} reasons={dict(reasons)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
