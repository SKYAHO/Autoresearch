# GCP 배포 (Cloud Run Job + Cloud Scheduler + GCS + BigQuery)

매일 09:00 KST 에 한국 YouTube 트렌딩을 수집해서
- **GCS** 에 일자별 parquet 저장: `gs://<버킷>/youtube_trending/year=YYYY/month=MM/youtube_trending_KR_YYYY-MM-DD.parquet`
- **BigQuery** 테이블에 append (`video_trending__date` 일자 파티션, 같은 날 재실행 시 교체)

## 구성

```
Cloud Scheduler (09:00 KST)  ──HTTP──▶  Cloud Run Job  ──▶  YouTube Data API
                                              │
                                              ├─▶  GCS (일자별 parquet)
                                              └─▶  BigQuery (append)
   API 키: Secret Manager
```

## 사전 준비 (환경 설정)

### 1) Google Cloud SDK(gcloud) 설치

```bash
# macOS - Homebrew
brew install --cask google-cloud-sdk
# 또는 공식 설치 스크립트
curl https://sdk.cloud.google.com | bash && exec -l $SHELL

gcloud --version    # gcloud, bq 표시되면 OK
```

### 2) 로그인 / 인증

```bash
gcloud auth login                       # 사용자 로그인
gcloud auth application-default login    # 로컬 ADC(빌드/배포용)
```

### 3) GCP 프로젝트 준비

프로젝트 ID는 **소문자/숫자/하이픈**만 가능합니다(대문자 불가). 결제 계정이 연결돼 있어야 합니다.

```bash
gcloud projects list                                  # 기존 프로젝트 확인
# 없으면 생성 후 콘솔에서 결제 연결
gcloud projects create my-autoresearch-123 --name="Autoresearch"
# 결제: https://console.cloud.google.com/billing
```

### 4) `gcp/deploy.sh` 상단 변수 수정

`PROJECT_ID`(위에서 확인/생성한 실제 ID), `BUCKET`(전역 유일), `YOUTUBE_API_KEY`(발급 키) 등을 본인 값으로 채웁니다.

## 빠른 시작

위 사전 준비를 마친 뒤, 프로젝트 루트에서:

```bash
bash gcp/deploy.sh
```

스크립트가 API 활성화 → 버킷/데이터셋/시크릿 생성 → 서비스계정·권한 →
이미지 빌드 → Cloud Run Job 배포 → Cloud Scheduler(매일 9시 KST) 등록까지 수행합니다.

## 수동 실행 / 확인

```bash
# 즉시 한 번 실행
gcloud run jobs execute youtube-trending-daily --region=asia-northeast3

# 로그
gcloud run jobs executions list --job=youtube-trending-daily --region=asia-northeast3

# BigQuery 확인
bq query --use_legacy_sql=false \
  'SELECT video_trending__date, COUNT(*) FROM `PROJECT.youtube.trending` GROUP BY 1 ORDER BY 1 DESC LIMIT 7'
```

## 기존 과거 데이터(마스터 parquet) 적재

로컬 `data/youtube_trending_videos_global.parquet`(과거 KR 시계열)을 한 번에 BigQuery로 올리려면:

```bash
# GCS 업로드 후 로드 (스키마 자동감지)
gcloud storage cp data/youtube_trending_videos_global.parquet gs://<버킷>/backfill/
bq load --source_format=PARQUET --replace=false \
  PROJECT:youtube.trending gs://<버킷>/backfill/youtube_trending_videos_global.parquet
```

> parquet 타입(Int64/datetime/boolean)이 BigQuery 스키마와 호환됩니다.
> 날짜 컬럼이 TIMESTAMP 로 들어가면 파티션 정의와 다를 수 있으니, 깔끔히 하려면
> 적재 전 `video_trending__date` 를 DATE 로 캐스팅하거나 staging 테이블을 거쳐 INSERT 하세요.

## 비용 메모

- Cloud Run Job: 하루 수십 초 실행 → 사실상 무료 티어 수준.
- GCS/BigQuery: 데이터량이 작아 월 몇백 원 내외.
- Cloud Scheduler: 작업 3개까지 무료.
