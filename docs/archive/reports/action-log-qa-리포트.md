# action log 생성 QA 리포트 (Phase 1, long event stream)

- 측정일: 2026-07-06 · 이슈 #57 · 포맷: **long 이벤트 스트림**(event_type 4종)
- 모델 2종(OpenRouter): `mistralai/mistral-nemo`, `qwen/qwen-2.5-72b-instruct`
- 입력: virtual_users 10명(`data/generated/virtual_users_qa_10.jsonl`) × KR TrendingVideo 200건(asaniczka, `data/raw/youtube/kr_trending_sample_200.parquet`)
- 조건: candidates_per_user=24, target_ctr=0.02, exploration_ratio=0.2, history 30일, seed=42
- 실행: 유저별 LLM 판정 호출을 ThreadPoolExecutor(10 workers)로 **병렬화**. draft 파싱·전역 2% 정규화·이벤트 확장은 유저 안정 순서로 단일 스레드 처리 → 응답 순서와 무관하게 결정론적.
- 설계: `docs/archive/specs/2026-07-06-event-log-long-format-design.md`

## 결과 비교

| 항목 | mistral-nemo | qwen-2.5-72b |
|---|---|---|
| impression 행 | 240 | 240 |
| click / view / like 행 | 5 / 5 / 5 | 5 / 5 / 5 |
| 총 event | 255 | 255 |
| **전역 CTR** (click/impression) | **2.08%** (목표 2.00%) | **2.08%** (목표 2.00%) |
| view watch_time (min/median/mean/max, s) | 133 / 689 / 580 / 720 | 199 / 338 / 431 / 792 |
| 격리 유저 | 0/10 | 0/10 |
| timestamp 범위 | 2026-06-06 09:26 ~ 2026-07-05 03:37 UTC | 2026-06-06 09:28 ~ 2026-07-05 03:39 UTC |
| LLM 판정 wall time (10 병렬) | 32.7s | 60.0s |
| 산출 parquet | `asset/action_log/event_log_mistral_nemo.parquet` | `asset/action_log/event_log_qwen2_5_72b.parquet` |

두 모델 모두 코드가 강제한 **정확히 2% CTR(5/240)** 을 지켰다. 클릭 대상 영상과 watch_time 분포는 모델마다 다르다 — LLM은 propensity/watch_fraction만 판단하고 클릭 비율은 코드가 결정하기 때문(설계 의도). 관측상 두 모델 모두 페르소나 primary_categories(Gaming/Music/Entertainment)에 맞는 K-POP MV·LCK/MSI 경기로 클릭이 몰렸다.

## 클릭 세션 샘플

**mistral-nemo**
- `vu_0005` → 'ATEEZ(에이티즈) - BAD Official MV' · view 689s · like
- `vu_0005` → 'BAD (LEEZ Ver.)' · view 133s · like
- `vu_0009` → '[MV] 태연 (TAEYEON)_ 만찬가' · view 670s · like
- `vu_0009` → 'ATEEZ - BAD Official MV' · view 689s · like
- `vu_0009` → 'BAD' · view 720s · like

**qwen-2.5-72b**
- `vu_0009` → 'Stray Kids "RUN IT" M/V' · view 338s · like
- `vu_0009` → '𝗩𝟴 singasong Official MV' · view 792s · like
- `vu_0010` → 'T1 vs KC - TLAW vs DCG | 플레이-인 2라운드 | MSI' · view 488s · like
- `vu_0010` → 'FUR vs LYON - T1 vs BLG | 승자조 1라운드 | MSI' · view 199s · like
- `vu_0010` → 'T1 vs TLAW - DCG vs KC | 플레이-인 1라운드 | MSI' · view 336s · like

## 판정

- ✅ long 스키마 준수: parquet 컬럼 `event_id, event_timestamp, user_id, event_type, video_id, watch_time_sec, rank, source`(+메타 4). `clicked/liked/search_keyword/exposure_type` 없음.
- ✅ 불변식: `view` 행만 `watch_time_sec` non-null, 그 외 null · 전 행 `rank=null`, `source=historical`.
- ✅ 세션 timestamp 단조: impression < click < view < like (같은 (user,video) 묶음).
- ✅ 전역 CTR ≈ 2% (양 모델 2.08%) · timestamp 30일 window 내 · 유저 격리 정상(전량 실패 아님).

## 운영 메모

- `qwen-2.5-72b`는 초기 실행에서 전량 `api_error` 격리됨 → 원인은 코드가 아니라 OpenRouter가 `response_format: json_object` 미지원 provider(Novita)로 라우팅해 400 반환(+DeepInfra 429). 장애 격리·전량실패 가드가 설계대로 10/10을 잡아냄. QA 러너에서 `json_object` 강제를 제거하고 프롬프트 기반 JSON + 펜스 제거 후처리로 재실행하여 정상 생성.
- 재현: `OPENROUTER_API_KEY`(Windows User 환경변수) 주입 후 QA 러너 실행. 병렬 호출로 모델당 ~30–60s.
