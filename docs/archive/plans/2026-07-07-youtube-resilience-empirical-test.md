# YouTube 복원력 실증 테스트 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 격리된 GCP 프로젝트(`autoresearch-501004`)에서 Cloud Run proxy를 배포하고, 정상 동작을 확인한 뒤, IP밴을 유도해 egress IP 회전을 실증한다.

**Architecture:** Cloud Run(`youtube-proxy`, FastAPI dumb forwarder) 경유로 YouTube Data API v3를 폭주 호출하여 Cloud Run egress IP 밴을 유도. revision 재배포로 egress IP가 교체됨(IP₁→IP₂)을 관측해 회전을 증명한다. proxy에 임시 `/egress-ip` 엔드포인트를 추가해 egress IP를 직접 조회한다.

**Tech Stack:** gcloud CLI, Cloud Run, FastAPI(`proxy/app.py`), Python(requests), YouTube Data API v3

## Global Constraints

(출처: spec `2026-07-07-youtube-resilience-empirical-test.md`)

- GCP 프로젝트: `autoresearch-501004` (#990733524187). 운영 프로젝트 `ar-infra-501607` 건드리지 말 것.
- gcloud 활성 계정: `78ffb92c81bc` (접근 권한 확인됨). 인증 만료 시 `gcloud auth login`.
- 리전: `asia-northeast3` (서울).
- Cloud Run 서비스명: `youtube-proxy`.
- 일일 quota: 10,000 units (videos.list = 1 unit/호출). quota는 프로젝트 단위라 운영 무영향.
- API Key: 환경 변수 `YOUTUBE_API_KEY` (사용자 제공, autoresearch-501004 프로젝트의 Key).
- 테스트 종료 후: Cloud Run 서비스 삭제, `proxy/app.py` 임시 변경 revert.
- 마스킹 불변량: Key값/헤더/본문/traceback/URL 전체 로그 금지. 로그에는 status code와 reason만.
- 응답/커밋 메시지/로그: 한국어 격식체. 식별자: 영어.

## File Structure

- **Modify (임시, 테스트 후 revert):** `proxy/app.py` — `/egress-ip` 엔드포인트 추가. Cloud Run egress IP 직접 조회용. PR #56 범위 밖 임시 변경이므로 별도 커밋 금지(작업 트리에서만 유지, 테스트 종료 후 revert).
- **Create:** `scripts/empirical_test/quota_burst.py` — proxy 경유 YouTube API 폭주 요청 스크립트. egress IP 밴 유도용. 일회성 스크립트(저장소 보관, 테스트 재현용).
- **Create:** `docs/archive/reports/2026-07-07-empirical-test-run-log.md` — 실행 로그. 각 Phase 결과(status code, reason, egress IP, 관측 사항)를 순차 기록. 최종 산출물.

**설계 빈틈 메모:** spec에는 egress IP 조회 방법이 구체화되어 있지 않다. 본 plan은 proxy에 임시 `/egress-ip` 엔드포인트(외부 IP 확인 서비스 호출)를 추가하는 방식으로 채운다. 사용자 검토 시 대안(예: Cloud Run 로그 기반 추론, 별도 Job)으로 변경 가능.

---

## Task 1: proxy 임시 debug 엔드포인트 + Phase 1 Cloud Run 배포

**목적:** proxy 컨테이너를 Cloud Run에 배포하고, egress IP₁을 기록한다.

**Files:**
- Modify: `proxy/app.py` (`/egress-ip` 엔드포인트 추가, 임시)
- Create: `docs/archive/reports/2026-07-07-empirical-test-run-log.md`

**Interfaces:**
- Consumes: `proxy/Dockerfile`, `proxy/app.py` (PR #56 산출물)
- Produces: Cloud Run 서비스 URL(환경 변수 `PROXY_URL`), egress IP₁

- [ ] **Step 1: proxy/app.py에 /egress-ip 임시 엔드포인트 추가**

`proxy/app.py`의 `/health` 엔드포인트 뒤에 추가:

```python
@app.get("/egress-ip")
def egress_ip():
    """Cloud Run egress IP 반환 (실증 테스트용 임시 엔드포인트, 테스트 후 revert)."""
    import urllib.request

    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=10) as r:
            return {"ip": r.read().decode().strip()}
    except Exception as e:
        return JSONResponse(
            status_code=502, content={"error": type(e).__name__}
        )
```

- [ ] **Step 2: gcloud 프로젝트 설정**

Run:
```bash
gcloud config set project autoresearch-501004
gcloud config get-value project
```
Expected: `autoresearch-501004`

- [ ] **Step 3: Cloud Run 서비스 배포**

Run:
```bash
gcloud run deploy youtube-proxy \
  --source proxy/ \
  --region asia-northeast3 \
  --port 8080 \
  --min-instances 0 \
  --max-instances 3 \
  --memory 256MiB \
  --cpu 1 \
  --no-allow-unauthenticated \
  --quiet
```
Expected: 배포 성공 메시지 + 서비스 URL 출력. URL을 환경 변수로 설정:
```bash
export PROXY_URL="<출력된 서비스 URL>"
```

- [ ] **Step 4: /health 확인**

Run:
```bash
gcloud run services describe youtube-proxy --region asia-northeast3 --format='value(status.url)'
```
URL 획득 후(위 PROXY_URL과 동일):
```bash
curl -sS -H "Authorization: bearer $(gcloud auth print-identity-token)" "$PROXY_URL/health"
```
Expected: `{"status":"ok"}`

- [ ] **Step 5: /egress-ip 확인 + IP₁ 기록**

Run:
```bash
curl -sS -H "Authorization: bearer $(gcloud auth print-identity-token)" "$PROXY_URL/egress-ip"
```
Expected: `{"ip":"<IPv4 주소>"}` — 이 값을 IP₁으로 기록.

- [ ] **Step 6: run-log.md 초기화 + Phase 1 결과 기록**

`docs/archive/reports/2026-07-07-empirical-test-run-log.md` 생성:

```markdown
# YouTube 복원력 실증 테스트 실행 로그

> Date: 2026-07-07 | Project: autoresearch-501004 | 담당: (사용자)

## Phase 1: Cloud Run proxy 배포

- 서비스명: youtube-proxy
- 리전: asia-northeast3
- 서비스 URL: `<PROXY_URL>`
- /health 응답: `{"status":"ok"}` ✅
- egress IP₁: `<IP₁ 값>`

### 비고

- (배포 중 이상/특이사항 기록)
```

실제 값으로 치환하여 작성.

---

## Task 2: Phase 2 정상 작동 확인

**목적:** IP밴 유도 전, 정상 조건에서 client.py + proxy가 의도대로 동작함을 확인한다.

**Prerequisite:**
- 환경 변수 `YOUTUBE_API_KEY` 설정 (사용자 제공)
- Task 1 완료 (`PROXY_URL` 설정됨)

**Files:**
- Modify: `docs/archive/reports/2026-07-07-empirical-test-run-log.md` (Phase 2 섹션 추가)

**Interfaces:**
- Consumes: `PROXY_URL`, `YOUTUBE_API_KEY`, `autoresearch/youtube_collection/client.py`
- Produces: 정상 동작 확인 결과(Phase 3 진행 전제)

- [ ] **Step 1: API Key 유효성 직접 확인**

Run:
```bash
curl -sS "https://www.googleapis.com/youtube/v3/videos?part=snippet&id=jNQXAC9IVRw&key=$YOUTUBE_API_KEY" | python -c "import sys,json; d=json.load(sys.stdin); print('status:', 'OK' if 'items' in d else d.get('error',{}).get('errors',[{}])[0].get('reason','?'))"
```
Expected: `status: OK`

- [ ] **Step 2: proxy 경유 직접 요청 → 200 확인**

Run:
```bash
curl -sS -H "Authorization: bearer $(gcloud auth print-identity-token)" \
  -H "X-Goog-Api-Key: $YOUTUBE_API_KEY" \
  "$PROXY_URL/youtube/v3/videos?part=snippet&id=jNQXAC9IVRw" \
  | python -c "import sys,json; d=json.load(sys.stdin); print('status:', 'OK' if 'items' in d else d.get('error',{}).get('errors',[{}])[0].get('reason','?'))"
```
Expected: `status: OK`

- [ ] **Step 3: client.py 정상 동작 확인 (proxy_url 구성)**

Run (인터프리터 1회 실행). `ResilientYouTubeClient.make_callables()`가 반환한 `YouTubeCallables.list_videos` callable 사용:
```bash
python -c "
from autoresearch.youtube_collection.client import ResilientYouTubeClient
c = ResilientYouTubeClient(
    keys=[__import__('os').environ['YOUTUBE_API_KEY']],
    proxy_url=__import__('os').environ['PROXY_URL'],
)
callables = c.make_callables()
r = callables.list_videos(part='snippet', id='jNQXAC9IVRw')
print('client.py OK' if 'items' in r else f'FAIL: {r}')
"
```
Expected: `client.py OK`

- [ ] **Step 4: run-log.md에 Phase 2 결과 기록**

`docs/archive/reports/2026-07-07-empirical-test-run-log.md`에 추가:

```markdown
## Phase 2: 정상 작동 확인

- API Key 직접 요청: ✅ (items 반환)
- proxy 경유 직접 요청: ✅ (items 반환)
- client.py (proxy_url 구성): ✅ (정상 동작)

### 비고

- (관찰 사항 기록)
```

---

## Task 3: Phase 3 IP밴 유도 시도

**목적:** Cloud Run egress IP 밴을 유도하고, revision 재배포로 egress IP 회전(IP₁→IP₂)을 증명한다.

**Prerequisite:**
- Task 2 완료 (정상 동작 확인)
- 환경 변수 `YOUTUBE_API_KEY`, `PROXY_URL` 설정

**Files:**
- Create: `scripts/empirical_test/quota_burst.py`
- Modify: `docs/archive/reports/2026-07-07-empirical-test-run-log.md` (Phase 3 섹션)

**Interfaces:**
- Consumes: `PROXY_URL`, `YOUTUBE_API_KEY`, Task 1의 IP₁
- Produces: IP밴 유도 결과(성공: IP₂ + 회복 확인 / 실패: 사용자 보고)

- [ ] **Step 1: quota_burst.py 작성**

`scripts/empirical_test/quota_burst.py` 생성:

```python
"""Cloud Run proxy 경유 YouTube API 폭주 요청 스크립트.

목적: Cloud Run egress IP 밴 유도. 직접(로컬) 호출이 아닌 proxy 경유로
호출하여 Cloud Run 의 egress IP 가 제재 대상이 되도록 한다.

환경 변수:
    QUOTA_BURST_PROXY_URL: proxy 서비스 URL
    YOUTUBE_API_KEY: YouTube Data API v3 Key
    QUOTA_BURST_MAX_CALLS: 최대 호출 수 (기본 10000 = 일일 quota 상한)
"""
import os
import sys
from collections import Counter

import requests

PROXY_URL = os.environ["QUOTA_BURST_PROXY_URL"]
API_KEY = os.environ["YOUTUBE_API_KEY"]
MAX_CALLS = int(os.environ.get("QUOTA_BURST_MAX_CALLS", "10000"))
VIDEO_ID = "jNQXAC9IVRw"
ENDPOINT = f"{PROXY_URL.rstrip('/')}/youtube/v3/videos"


def main() -> int:
    stats: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    token = __import__("subprocess").check_output(
        ["gcloud", "auth", "print-identity-token"]
    ).decode().strip()
    for i in range(1, MAX_CALLS + 1):
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
            stats["network_error"] += 1
            if i % 100 == 0:
                print(f"[{i}/{MAX_CALLS}] network_error: {type(e).__name__}", file=sys.stderr)
            continue
        stats[resp.status_code] += 1
        if resp.status_code != 200:
            try:
                reason = resp.json().get("error", {}).get("errors", [{}])[0].get("reason", "?")
            except ValueError:
                reason = "(non-JSON)"
            reasons[reason] += 1
        if i % 100 == 0:
            print(f"[{i}/{MAX_CALLS}] stats={dict(stats)} reasons={dict(reasons)}", flush=True)
        # IP밴 시그니처 의심: 403 비율 급증 시 조기 종료
        if stats[403] >= 50 and stats[403] > stats.get(200, 0):
            print(f"IP밴 시그니처 의심 — 403 {stats[403]}건, 조기 종료", flush=True)
            break
    print(f"FINAL stats={dict(stats)} reasons={dict(reasons)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: 폭주 실행 — proxy 경유 YouTube API 호출**

Run:
```bash
export QUOTA_BURST_PROXY_URL="$PROXY_URL"
python scripts/empirical_test/quota_burst.py 2>&1 | tee /tmp/quota_burst.log
```
Expected: 진행 로그(`[N/10000] stats=...`) 출력 후 `FINAL stats=... reasons=...`. 주요 관찰 대상:
- `quotaExceeded` (403) 비율 — quota 소진 시점
- 403 이유 분포 변화 — `quotaExceeded` 외 403(userIP 제재 시사) 발생 여부

- [ ] **Step 3: IP밴 시그니처 감지 판정**

`/tmp/quota_burst.log`의 `FINAL` 라인 분석. 다음 중 하나 이상 시 "IP밴 발생" 판정:
- 403 reason이 `quotaExceeded`가 아닌 값(userIP suspended, bannedIp 등) 다수
- client.py IP밴 시그니처(전 활성 Key 동일 403) — Key 1개 환경에서는 판정 제한적
- quota 소진 후에도 403 지속

**판정 분기:**
- IP밴 감지 → Step 4 (회전 증명)
- IP밴 미감지 → Step 5 (사용자 보고)

- [ ] **Step 4 (IP밴 감지 시): revision 재배포 + IP₂ 관측 + 회복 확인**

Run (트래픽 100% 신규 revision으로 재배포):
```bash
gcloud run deploy youtube-proxy \
  --source proxy/ \
  --region asia-northeast3 \
  --no-traffic \
  --quiet
# 최신 revision을 조회 후 트래픽 100% 전환
REVISION=$(gcloud run revisions list --service youtube-proxy --region asia-northeast3 --format='value(name)' --limit=1)
gcloud run services update-traffic youtube-proxy --region asia-northeast3 --to-revisions="$REVISION=100" --quiet
```

egress IP₂ 확인:
```bash
curl -sS -H "Authorization: bearer $(gcloud auth print-identity-token)" "$PROXY_URL/egress-ip"
```
Expected: IP₁과 다른 IP₂.

회복 확인 (IP₂에서 YouTube API 200):
```bash
curl -sS -H "Authorization: bearer $(gcloud auth print-identity-token)" \
  -H "X-Goog-Api-Key: $YOUTUBE_API_KEY" \
  "$PROXY_URL/youtube/v3/videos?part=snippet&id=jNQXAC9IVRw"
```
Expected: quota가 남아 있으면 items 반환(200). quota 소진 상태면 `quotaExceeded` — 이 경우 회전 증명은 IP₁≠IP₂ 로 충분.

run-log.md에 기록 → Step 6.

- [ ] **Step 5 (IP밴 미감지 시): 사용자 보고**

사용자에게 보고 내용:
- 시도한 호출 수, 응답 분포(`stats`, `reasons`)
- quota 소진 시점
- IP밴 시그니처 미관측

사용자 결정 대기:
- (a) 추가 시도 — quota 리셋(자정 KST) 후 재시도
- (b) 접근 A 폴백 — quota 초과 실증 + 회전은 revision 배포로 별도 증명 + IP밴/프록시는 단위테스트 시연
- (c) 데모 취소 — "정상 사용 환경에서 IP밴은 발생하지 않는다"는 결과를 발견으로 문서화

→ 결정 후 Step 6.

- [ ] **Step 6: run-log.md에 Phase 3 + 최종 결과 기록**

`docs/archive/reports/2026-07-07-empirical-test-run-log.md`에 추가:

```markdown
## Phase 3: IP밴 유도 시도

- 시도 호출 수: `<N>`
- 응답 분포(stats): `<dict>`
- 응답 분포(reasons): `<dict>`
- IP밴 감지: (예/아니오)
- (감지 시) egress IP₁ → IP₂: `<IP₁>` → `<IP₂>`
- (감지 시) 회복 확인: (200 회복 / quota 소진으로 확인 불가)
- (미감지 시) 사용자 결정: (a/b/c)

## 최종 결론

- 회전 증명: (성공/실패/불가)
- 핵심 발견: (한 줄 요약)

### 산출 파일

- `/tmp/quota_burst.log` (폭주 요청 로그)
```

- [ ] **Step 7: 테스트 종료 정리**

Cloud Run 서비스 삭제:
```bash
gcloud run services delete youtube-proxy --region asia-northeast3 --quiet
```

`proxy/app.py` 임시 변경 revert (`/egress-ip` 엔드포인트 제거):
```bash
git checkout -- proxy/app.py
```
Expected: `git diff proxy/app.py` 출력 없음.

`scripts/empirical_test/quota_burst.py`, `docs/archive/reports/2026-07-07-empirical-test-run-log.md`는 보관(재현/기록용). 커밋 여부는 사용자 결정.

---

## Self-Review (작성자 점검)

**1. Spec coverage:**
- Phase 1 (Cloud Run 배포, /health, egress IP₁) → Task 1 ✅
- Phase 2 (Key 유효성, client.py 정상, proxy 경유) → Task 2 ✅
- Phase 3 (폭주, 비공식 패턴, 계속 시도, 안 걸리면 보고, 감지 시 회전 증명) → Task 3 ✅
- 산출물(문서 + 로그) → run-log.md ✅
- 제약(운영 무영향, 비용, Key 분리) → Global Constraints ✅
- spec의 "비공식 패턴 병행"(인증 없는 직접 호출)은 quota 소진 후 옵션으로 남김 — Task 3 Step 3 판정 후 사용자 결정 시 언급. (spec 121-123행)

**2. Placeholder scan:**
- `<PROXY_URL>`, `<IP₁ 값>` 등은 런타임 치환 표시(placeholder 아님). 실제 값으로 채워지는 실행 지점.
- 빈틈 메모(egress IP 조회 방법)는 의도적 명시, 사용자 검토 대상.

**3. Type/이름 일치:**
- 환경 변수: `PROXY_URL`, `YOUTUBE_API_KEY`, `QUOTA_BURST_PROXY_URL`, `QUOTA_BURST_MAX_CALLS` — 일관.
- 엔드포인트: `/health`, `/egress-ip`, `/youtube/v3/videos` — 일관.
- 서비스명 `youtube-proxy` — 일관.

**4. 알려진 제한:**
- Key 1개 환경에서는 client.py IP밴 시그니처(전 활성 Key 동일 403, 최소 Key≥2) 판정이 제한적. stats/reasons 분포로 대체 판정(Task 3 Step 3).
- Cloud Run egress IP는 Google 공유 IP 풀에서 할당. `/egress-ip`가 반환하는 IP는 요청 시점의 IP이며, revision 간 다를 수 있음.
