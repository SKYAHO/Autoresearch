# action log 생성 QA 리포트 (Phase 1, MVP)

> ⚠️ **STALE (wide 포맷) — 2026-07-06 event log long 전환(이슈 #57) 이전 결과입니다.**
> 아래 지표는 `clicked`/`liked`/`exposure_type` 등 제거된 wide 컬럼 기준이라 현재 스키마
> (`autoresearch/action_logs/schema.py`, event_type 4종)와 일치하지 않습니다. long 포맷
> QA 재생성은 `OPENROUTER_API_KEY` 확보 후 수행 예정(플랜 Task 6). 설계:
> `docs/superpowers/specs/2026-07-06-event-log-long-format-design.md`.

- 측정일: 2026-07-06 · 이슈 #57 · 모델 `mistralai/mistral-nemo`(OpenRouter)
- 입력: virtual_users 10명(vu_nemo_10) × KR TrendingVideo 200건(asaniczka)
- 조건: candidates_per_user=24, target_ctr=0.02, exploration_ratio=0.2, history 30일, seed=42

## 결과

| 항목 | 값 |
|---|---|
| 총 event(impression) | 240 |
| clicked=1 | 5 |
| **전역 CTR** | **2.08%** (목표 2.00%) |
| 격리 유저 | 0/10 (api 0/json 0/schema 0) |
| clicked=0 제약 위반 | 0 (watch/like≠0) |
| timestamp 범위 | 2026-06-06 00:03:59+00:00 ~ 2026-07-04 21:39:10+00:00 |
| exposure_type 분포 | {'top_ranked': 190, 'exploration': 50} |
| 유저별 노출 수 | min 24 / max 24 |
| 호출당 지연 | 평균 33.3s (min 25.0~max 41.0) |
| 총 소요 | 332.6s |

## 클릭된 event 샘플 (최대 8건)

- `vu_0006` → 'FUR vs LYON - T1 vs BLG | 브래킷 스테이지 승자조 1라운드 | MSI ' | watch 256s liked 1 exposure top_ranked kw 'LCK'
- `vu_0006` → 'HLE vs TSW - G2 vs TES | 브래킷 스테이지 승자조 1라운드 | MSI 2' | watch 346s liked 1 exposure top_ranked kw 'LCK'
- `vu_0006` → 'T1 vs TLAW - DCG vs KC | 플레이-인 스테이지 1라운드 | MSI 202' | watch 432s liked 1 exposure top_ranked kw 'LCK'
- `vu_0006` → 'T1 vs TLAW | 플레이-인 스테이지 4라운드 | MSI 2026' | watch 372s liked 1 exposure top_ranked kw 'LCK'
- `vu_0006` → 'T1 vs KC - TLAW vs DCG | 플레이-인 스테이지 2라운드 | MSI 202' | watch 627s liked 1 exposure top_ranked kw 'LCK'

## 판정

- ✅ 스키마 준수(EventLog 검증 통과)
- ✅ clicked=0 ⇒ watch/like=0
- ✅ 전역 CTR ≈ 2% (2.08%)
- ✅ timestamp 30일 window 내
- ✅ 유저 격리 동작(전량 실패 아님)
