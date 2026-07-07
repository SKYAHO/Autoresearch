# 대규모 생성 리포트 — virtual_user 6.9k → action log 70만

- 일자: 2026-07-07 · 이슈 #64 (이슈 #57 후속) · 모델: OpenRouter `mistralai/mistral-nemo`
- 목적: persona → virtual_users → action log 파이프라인을 실제 스케일로 검증·생성.

## 산출물 (로컬, gitignore)
| 파일 | 내용 |
|---|---|
| `asset/virtual_user/vu_1000.parquet` | virtual_user **6,983명** (20대 3,899 / 30대 2,748 / 40세 336) |
| `asset/action_log/event_log_all.parquet` | action log **702,524 event** |
| `data/raw/youtube/kr_trending_2000.parquet` | KR 영상 2,000개(후보 풀) |

## 결과 요약
```
action log: 702,524 event
  impression 662,808 / click 13,256 / view 13,256 / like 13,204
CTR: 2.00% (코드 강제) · event_id 전부 고유 · 영상 2,000개 전부 커버
연령대별: 20대 406,309 / 30대 264,092 / 40대 32,123 event
```

## 방법 (재현)

### 1) 영상 풀 추출 (2,000 KR)
backfill CSV(113개국 ~7.6GB)에서 DuckDB 스트리밍으로 KR·dedup·조회수 상위 2,000:
```
python scripts/extract_kr_videos.py --count 2000 --out data/raw/youtube/kr_trending_2000.parquet
```

### 2) virtual_user 생성 (다배치 이어붙이기)
`OpenRouterVirtualUserGenerator`(#59, 검증·병렬 포함)로 배치 생성 후 하나의 parquet에 이어붙임.
- **id 충돌 방지**: 각 배치 `user_id`를 이전 max 인덱스만큼 offset (`vu_1101+`, `vu_4401+`).
- **persona 중복 방지**: 배치마다 Nemotron 다른 파일 사용(file0/1/2) → 같은 사람 재샘플 방지.
- **연령**: `age_min/age_max`로 20대 배치와 30-40세 배치를 나눠 생성 → 연령 다양성.

| 배치 | 나이 | persona | 유효 |
|---|---|---|---|
| 원본 | 20-29 | file0(앞 6만) | 989 |
| batch2 | 20-29 | file1 | 2,910 |
| batch3 | 30-40 | file2 | 3,084 |

### 3) action log 생성 (청킹 + 병렬)
`scripts/generate_action_logs_scale.py` — 후보 96개를 24×4 청킹, 동시성 60으로 병렬 생성.
신규 유저만 처리하고 event_id offset으로 기존 로그에 이어붙임:
```
python scripts/generate_action_logs_scale.py \
  --users asset/virtual_user/vu_1000.parquet --min-user-index 1100 \
  --videos data/raw/youtube/kr_trending_2000.parquet \
  --candidates 96 --chunk 24 --concurrency 60 \
  --out asset/action_log/event_log_batch2.parquet --event-offset 100000
```
그다음 기존 `event_log_100k.parquet`와 concat → `event_log_all.parquet`.

## 핵심 설계 포인트
- **청킹**: 유저당 후보 96개를 한 콜에 넣으면 약한 모델(nemo)이 응답을 truncate → 격리 폭증.
  24개씩 쪼개 독립 콜로 처리하면 콜당 context가 작아 품질·안정성↑, **청크 단위 격리**로
  한 청크 실패가 유저 전체를 죽이지 않음.
- **병렬**: (유저×청크) 콜을 ThreadPool로 동시 실행. id·조립·검증은 원본 순서 유지 →
  병렬이어도 결정론.
- **CTR 고정**: LLM은 propensity/watch_fraction/would_like만 판단, 코드가 전역 상위 2%를
  클릭으로 선정 → 모델 무관 CTR 2% 고정.

## 성능 (실측, mistral-nemo)
| 단계 | 규모 | 시간 | 비용(추정) |
|---|---|---|---|
| 영상 추출 | 7.6GB→2,000 | ~5분 | — |
| vu 생성(batch2+3) | ~6,600콜 | ~1시간 | ~$0.3 |
| action log 생성 | 23,976콜(동시성60) | ~4시간 | ~$1.7 |

## 검증
- CTR 정확히 2.00%, event_type 구조·불변식(view만 watch_time·rank null·source historical) 정상.
- event_id·user_id 전부 고유(배치 offset), 영상 2,000개 전부 커버.
- 격리는 청크 단위(api_error/invalid_json 소수) → 유저 손실 최소(6,982/6,983 참여).

## 다음 레이어 (범위 밖)
CTR training dataset — `impression LEFT JOIN click`로 clicked 라벨, view/like/click 집계로
dynamic feature → 개인화 reranking ML.
