# YouTube Trending 수집 파이프라인

YouTube Data API v3로 한국(KR) 인기 급상승 트렌딩을 수집해 parquet으로 저장하고,
매일 누적(append)한다. 로컬 실행과 GCP(Cloud Run + Scheduler + GCS + BigQuery) 배포를 지원.

## 구성

```
fetch_trending_dataset.py   # 트렌딩 수집 핵심 로직(영상+채널, 타입 지정 DataFrame)
run_daily.py                # 매일 KR 수집 → 일자별 parquet + 마스터 append (로컬)
gcp/                        # Cloud Run Job + Scheduler + GCS + BigQuery 배포 일체
data/                       # 산출물(깃 제외): 마스터 parquet, 연/월/일 일자 파일
```

## 설치

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

## API 키

[Google Cloud Console](https://console.cloud.google.com/)에서 발급한 YouTube Data API v3 키를
환경변수 또는 `.env`에 둔다.

```bash
export YOUTUBE_API_KEY="발급받은_키"
# 또는 .env 파일에:  YOUTUBE_API_KEY=발급받은_키
```

## 로컬 실행

```bash
# 한국 트렌딩 수집 → data/<연>/<월>/youtube_trending_KR_<날짜>.parquet 저장 + 마스터 append
.venv/bin/python run_daily.py

# 임의 국가/포맷으로 수집(테스트)
.venv/bin/python fetch_trending_dataset.py --regions KR,US,JP --max 200
```

`run_daily.py`는 같은 날짜로 다시 실행하면 그 날짜 데이터를 교체하므로 중복이 쌓이지 않는다.

## 컬럼 (28개)

숫자는 `Int64`(결측 허용), 시각/날짜는 `datetime`, 불리언은 `boolean`, 나머지는 `string`.

| # | 컬럼 | 타입 | 설명 |
|---|------|------|------|
| 1 | `video_id` | string | 영상 고유 ID |
| 2 | `video_published_at` | datetime(UTC) | 영상 게시 시각 |
| 3 | `video_trending__date` | datetime(날짜) | 트렌딩 수집 날짜 |
| 4 | `video_trending_country` | string | 트렌딩 국가(정식 영문명, 예: South Korea) |
| 5 | `channel_id` | string | 채널 고유 ID |
| 6 | `video_title` | string | 영상 제목 |
| 7 | `video_description` | string | 영상 설명 |
| 8 | `video_default_thumbnail` | string | 기본 썸네일 URL |
| 9 | `video_category_id` | string | 카테고리 **이름**(예: Music) |
| 10 | `video_tags` | string | 태그(콤마 구분) |
| 11 | `video_duration` | string | 영상 길이(ISO 8601, 예: PT5M33S) |
| 12 | `video_dimension` | string | 화면 차원(2d/3d) |
| 13 | `video_definition` | string | 화질(hd/sd) |
| 14 | `video_licensed_content` | boolean | 라이선스 콘텐츠 여부 |
| 15 | `video_view_count` | Int64 | 영상 조회수 |
| 16 | `video_like_count` | Int64 | 좋아요 수 |
| 17 | `video_comment_count` | Int64 | 댓글 수 |
| 18 | `channel_title` | string | 채널명 |
| 19 | `channel_description` | string | 채널 설명 |
| 20 | `channel_custom_url` | string | 채널 커스텀 URL(@handle) |
| 21 | `channel_published_at` | datetime(UTC) | 채널 생성 시각 |
| 22 | `channel_country` | string | 채널 국가 |
| 23 | `channel_view_count` | Int64 | 채널 총 조회수 |
| 24 | `channel_subscriber_count` | Int64 | 구독자 수 |
| 25 | `channel_have_hidden_subscribers` | boolean | 구독자 수 숨김 여부 |
| 26 | `channel_video_count` | Int64 | 채널 총 영상 수 |
| 27 | `channel_localized_title` | string | 현지화 채널명 |
| 28 | `channel_localized_description` | string | 현지화 채널 설명 |

> 결측은 숫자=`NA`, 문자열=`<NA>`. `video_category_id`는 숫자 ID가 아니라 카테고리 이름이다.

## GCP 배포

매일 09:00 KST 자동 수집을 GCP에서 운영하려면 **사전 준비(gcloud 설치·인증·프로젝트 설정)부터
배포까지** [`gcp/README.md`](gcp/README.md)를 참고하세요.
(Cloud Run Job + Cloud Scheduler + GCS 일자 parquet + BigQuery 적재)
