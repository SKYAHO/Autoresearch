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

## 컬럼 타입

영상/채널 28개 컬럼 + 수집 메타. 숫자(view/like/comment·channel 통계)는 정수(Int64, 결측 NA),
시각(`*_published_at`)·날짜(`video_trending__date`)는 datetime, 공개여부·구독자숨김은 boolean,
나머지(제목·태그·duration 등)는 string.

## GCP 배포

매일 09:00 KST 자동 수집을 GCP에서 운영하려면 **사전 준비(gcloud 설치·인증·프로젝트 설정)부터
배포까지** [`gcp/README.md`](gcp/README.md)를 참고하세요.
(Cloud Run Job + Cloud Scheduler + GCS 일자 parquet + BigQuery 적재)
