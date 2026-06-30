#!/usr/bin/env bash
# Nemotron-Personas(ko_KR) 기반 가상 사용자(에이전트) 합성 대화 생성 환경을 GCP에 배포.
#   - 데이터: GCS + BigQuery(personas / persona_dialogues)
#   - 모델: Vertex AI Gemini
#   - 실행: Cloud Run Job(배치 생성) + Cloud Run Service(인터랙티브 API)
# 프로젝트 루트에서 실행:  bash persona-agents/deploy.sh
set -euo pipefail

# ── 0) 설정 — 본인 값으로 수정 ─────────────────────────────────
PROJECT_ID="autoresearch-501004"
GCP_REGION="asia-northeast3"            # Cloud Run/BQ/GCS 리전
VERTEX_LOCATION="us-central1"           # Gemini 가용 리전
GEMINI_MODEL="gemini-2.5-flash"
BUCKET="${PROJECT_ID}-persona-agents"
BQ_DATASET="persona"
PERSONAS_TABLE="${PROJECT_ID}.${BQ_DATASET}.personas"
INTERACTIONS_TABLE="${PROJECT_ID}.${BQ_DATASET}.interactions"
VIDEOS_TABLE="${PROJECT_ID}.youtube.trending"   # 후보 영상 카탈로그(기존 YouTube 파이프라인)
JOB_NAME="persona-sim"
SERVICE_NAME="persona-api"
IMAGE="${GCP_REGION}-docker.pkg.dev/${PROJECT_ID}/persona/agents:latest"

# 페르소나 데이터셋 출처(둘 중 하나). NGC/HF 페이지에서 ko_KR repo id 확인 후 채우세요.
PERSONA_HF_REPO="${PERSONA_HF_REPO:-}"          # 예: nvidia/Nemotron-Personas-Korea
PERSONA_HF_TOKEN="${PERSONA_HF_TOKEN:-}"        # 비공개면 토큰 필요
# .env 에서 HF 토큰 자동 로드
if [ -z "$PERSONA_HF_TOKEN" ] && [ -f .env ]; then
  PERSONA_HF_TOKEN="$(grep -E '^PERSONA_HF_TOKEN=' .env | head -n1 | cut -d= -f2- | tr -d '"'\''')"
fi

RUN_SA="persona-sa@${PROJECT_ID}.iam.gserviceaccount.com"
SCHED_SA="persona-sched-sa@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud config set project "$PROJECT_ID"

# ── 1) API 활성화 ──────────────────────────────────────────────
gcloud services enable \
  run.googleapis.com cloudscheduler.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com storage.googleapis.com bigquery.googleapis.com \
  secretmanager.googleapis.com aiplatform.googleapis.com

# ── 2) 저장소 준비 ─────────────────────────────────────────────
gcloud artifacts repositories create persona --repository-format=docker \
  --location="$GCP_REGION" 2>/dev/null || true
gcloud storage buckets create "gs://${BUCKET}" --location="$GCP_REGION" 2>/dev/null || true
bq --location="$GCP_REGION" mk -d "${PROJECT_ID}:${BQ_DATASET}" 2>/dev/null || true

# ── 3) (선택) HF 토큰 시크릿 ───────────────────────────────────
gcloud secrets create persona-hf-token 2>/dev/null || true
if [ -n "$PERSONA_HF_TOKEN" ]; then
  printf '%s' "$PERSONA_HF_TOKEN" | gcloud secrets versions add persona-hf-token --data-file=-
else
  echo "INFO: PERSONA_HF_TOKEN 미설정. 공개 데이터셋이면 불필요, 비공개면 나중에 추가하세요."
fi

# ── 4) 서비스 계정 + 권한 ──────────────────────────────────────
gcloud iam service-accounts create persona-sa --display-name="Persona agents" 2>/dev/null || true
gcloud iam service-accounts create persona-sched-sa --display-name="Persona scheduler" 2>/dev/null || true

# SA 전파 대기(생성 직후 IAM 바인딩 실패 방지)
for SA in "$RUN_SA" "$SCHED_SA"; do
  for _ in $(seq 1 12); do
    gcloud iam service-accounts describe "$SA" >/dev/null 2>&1 && break
    echo "  서비스 계정 전파 대기... ($SA)"; sleep 5
  done
done

gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${RUN_SA}" --role="roles/storage.objectAdmin"
for ROLE in roles/bigquery.dataEditor roles/bigquery.jobUser roles/aiplatform.user; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${RUN_SA}" --role="$ROLE" >/dev/null
done
gcloud secrets add-iam-policy-binding persona-hf-token \
  --member="serviceAccount:${RUN_SA}" --role="roles/secretmanager.secretAccessor" 2>/dev/null || true
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${SCHED_SA}" --role="roles/run.invoker" >/dev/null

# ── 5) 이미지 빌드 ─────────────────────────────────────────────
gcloud builds submit --config persona-agents/cloudbuild.yaml --substitutions=_IMAGE="$IMAGE" .

COMMON_ENV="GCP_PROJECT=${PROJECT_ID},VERTEX_LOCATION=${VERTEX_LOCATION},GEMINI_MODEL=${GEMINI_MODEL},BQ_LOCATION=${GCP_REGION},PERSONAS_TABLE=${PERSONAS_TABLE}"

# ── 6) 인터랙티브 API (Cloud Run Service) ──────────────────────
gcloud run deploy "$SERVICE_NAME" \
  --image="$IMAGE" --region="$GCP_REGION" --service-account="$RUN_SA" \
  --no-allow-unauthenticated \
  --set-env-vars="${COMMON_ENV}"

# ── 7) 행동 시뮬레이션 (Cloud Run Job) — command override 로 simulate_behavior 실행 ──
gcloud run jobs deploy "$JOB_NAME" \
  --image="$IMAGE" --region="$GCP_REGION" --service-account="$RUN_SA" \
  --max-retries=1 --task-timeout=3600s \
  --command="python" --args="simulate_behavior.py" \
  --set-env-vars="${COMMON_ENV},VIDEOS_TABLE=${VIDEOS_TABLE},OUTPUT_TABLE=${INTERACTIONS_TABLE},GCS_BUCKET=${BUCKET},SAMPLE_USERS=20,POOL_SIZE=80,SLATE_SIZE=15"

cat <<EOF

배포 완료.
다음 순서로 사용하세요:

1) 페르소나 적재(최초 1회) — repo id 를 채운 뒤:
   PERSONA_HF_REPO="nvidia/Nemotron-Personas-Korea" \\
   GCP_PROJECT=${PROJECT_ID} GCS_BUCKET=${BUCKET} BQ_TABLE=${PERSONAS_TABLE} \\
   BQ_LOCATION=${GCP_REGION} python persona-agents/ingest_personas.py
   (또는 Cloud Run Job 으로도 실행 가능)

2) 행동 시뮬레이션 실행(가상 유저 → 영상 클릭/시청 이벤트 생성):
   gcloud run jobs execute ${JOB_NAME} --region=${GCP_REGION}
   결과: BigQuery ${INTERACTIONS_TABLE} (학습용 상호작용 데이터)

3) (선택) 인터랙티브 페르소나 대화 API:
   URL=\$(gcloud run services describe ${SERVICE_NAME} --region=${GCP_REGION} --format='value(status.url)')
   curl -H "Authorization: Bearer \$(gcloud auth print-identity-token)" "\$URL/personas?n=3"

다음 단계(예정): interactions 로 추천 모델 학습(BQML → Vertex Two-Tower) + 추천 서빙.
EOF
