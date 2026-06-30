#!/usr/bin/env bash
# YouTube 트렌딩 일일 수집을 GCP(Cloud Run Job + Cloud Scheduler + GCS + BigQuery)에 배포.
# 프로젝트 루트에서 실행:  bash gcp/deploy.sh
set -euo pipefail

# ─────────────────────────────────────────────────────────────
# 0) 설정 — 본인 값으로 수정
# ─────────────────────────────────────────────────────────────
PROJECT_ID="Autoresearch"          # GCP 프로젝트 ID
GCP_REGION="asia-northeast3"         # 서울 리전
BUCKET="${PROJECT_ID}-youtube-trend" # GCS 버킷명(전역 유일)
BQ_DATASET="youtube"                 # BigQuery 데이터셋
BQ_TABLE_NAME="trending"             # BigQuery 테이블
JOB_NAME="youtube-trending-daily"    # Cloud Run Job 이름
IMAGE="${GCP_REGION}-docker.pkg.dev/${PROJECT_ID}/yt/${JOB_NAME}:latest"
YOUTUBE_API_KEY=""       # Secret Manager 에 넣을 값

RUN_SA="yt-job-sa@${PROJECT_ID}.iam.gserviceaccount.com"      # 작업 실행 SA
SCHED_SA="yt-sched-sa@${PROJECT_ID}.iam.gserviceaccount.com"  # 스케줄러 SA
BQ_TABLE="${PROJECT_ID}.${BQ_DATASET}.${BQ_TABLE_NAME}"

gcloud config set project "$PROJECT_ID"

# ─────────────────────────────────────────────────────────────
# 1) 필요한 API 활성화
# ─────────────────────────────────────────────────────────────
gcloud services enable \
  run.googleapis.com cloudscheduler.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com storage.googleapis.com bigquery.googleapis.com \
  secretmanager.googleapis.com

# ─────────────────────────────────────────────────────────────
# 2) 저장소 준비: Artifact Registry, GCS 버킷, BigQuery 데이터셋
# ─────────────────────────────────────────────────────────────
gcloud artifacts repositories create yt --repository-format=docker \
  --location="$GCP_REGION" 2>/dev/null || true
gcloud storage buckets create "gs://${BUCKET}" --location="$GCP_REGION" 2>/dev/null || true
bq --location="$GCP_REGION" mk -d "${PROJECT_ID}:${BQ_DATASET}" 2>/dev/null || true

# ─────────────────────────────────────────────────────────────
# 3) API 키를 Secret Manager 에 저장
# ─────────────────────────────────────────────────────────────
gcloud secrets create youtube-api-key 2>/dev/null || true
printf '%s' "$YOUTUBE_API_KEY" | gcloud secrets versions add youtube-api-key --data-file=-

# ─────────────────────────────────────────────────────────────
# 4) 서비스 계정 + 권한
# ─────────────────────────────────────────────────────────────
gcloud iam service-accounts create yt-job-sa --display-name="YT job" 2>/dev/null || true
gcloud iam service-accounts create yt-sched-sa --display-name="YT scheduler" 2>/dev/null || true

# 작업 SA: GCS 쓰기, BigQuery 적재/쿼리, 시크릿 읽기
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${RUN_SA}" --role="roles/storage.objectAdmin"
for ROLE in roles/bigquery.dataEditor roles/bigquery.jobUser; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${RUN_SA}" --role="$ROLE" >/dev/null
done
gcloud secrets add-iam-policy-binding youtube-api-key \
  --member="serviceAccount:${RUN_SA}" --role="roles/secretmanager.secretAccessor"

# 스케줄러 SA: Run Job 실행 권한
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SCHED_SA}" --role="roles/run.invoker" >/dev/null

# ─────────────────────────────────────────────────────────────
# 5) 이미지 빌드 (루트 컨텍스트 + gcp/Dockerfile)
# ─────────────────────────────────────────────────────────────
gcloud builds submit --config gcp/cloudbuild.yaml --substitutions=_IMAGE="$IMAGE" .

# ─────────────────────────────────────────────────────────────
# 6) Cloud Run Job 배포
# ─────────────────────────────────────────────────────────────
gcloud run jobs deploy "$JOB_NAME" \
  --image="$IMAGE" \
  --region="$GCP_REGION" \
  --service-account="$RUN_SA" \
  --max-retries=2 \
  --task-timeout=900s \
  --set-env-vars="GCS_BUCKET=${BUCKET},BQ_TABLE=${BQ_TABLE},BQ_LOCATION=${GCP_REGION},REGION_CODE=KR,MAX_RESULTS=200,YOUTUBE_API_KEY_SECRET=projects/${PROJECT_ID}/secrets/youtube-api-key/versions/latest"

# 한 번 수동 실행해서 동작 확인(선택)
# gcloud run jobs execute "$JOB_NAME" --region="$GCP_REGION"

# ─────────────────────────────────────────────────────────────
# 7) Cloud Scheduler: 매일 09:00 KST 에 Run Job 실행
# ─────────────────────────────────────────────────────────────
RUN_URI="https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${GCP_REGION}/jobs/${JOB_NAME}:run"
gcloud scheduler jobs create http "${JOB_NAME}-trigger" \
  --location="$GCP_REGION" \
  --schedule="0 9 * * *" \
  --time-zone="Asia/Seoul" \
  --uri="$RUN_URI" \
  --http-method=POST \
  --oauth-service-account-email="$SCHED_SA" \
  2>/dev/null || \
gcloud scheduler jobs update http "${JOB_NAME}-trigger" \
  --location="$GCP_REGION" \
  --schedule="0 9 * * *" \
  --time-zone="Asia/Seoul" \
  --uri="$RUN_URI" \
  --http-method=POST \
  --oauth-service-account-email="$SCHED_SA"

echo "완료. 매일 09:00 KST 에 ${JOB_NAME} 이 실행됩니다."
