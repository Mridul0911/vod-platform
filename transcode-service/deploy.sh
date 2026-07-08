#!/usr/bin/env bash
# deploy.sh — Deploy the transcode-mux service to Cloud Run
# Usage: ./deploy.sh [--project PROJECT_ID] [--region REGION]
#
# Requires: gcloud CLI authenticated with appropriate permissions.
# Set env vars or pass args; PROJECT_ID and REGION are mandatory.

set -euo pipefail

# ── Configurable defaults ──────────────────────────────────────────────────────
PROJECT_ID="${PROJECT_ID:-""}"
REGION="${REGION:-"us-central1"}"
SERVICE_NAME="${SERVICE_NAME:-"vod-transcode-mux"}"
IMAGE_NAME="${IMAGE_NAME:-"gcr.io/${PROJECT_ID}/${SERVICE_NAME}"}"
SERVICE_ACCOUNT="${SERVICE_ACCOUNT:-""}"          # Cloud Run runtime SA
PUBSUB_SA="${PUBSUB_SA:-""}"                       # Pub/Sub invoker SA
INPUT_BUCKET="${INPUT_BUCKET:-""}"                 # Raw media bucket
OUTPUT_BUCKET="${OUTPUT_BUCKET:-""}"               # Processed MP4 bucket
FIRESTORE_COLLECTION="${FIRESTORE_COLLECTION:-"content"}"
AUDIO_BITRATE="${AUDIO_BITRATE:-"128k"}"
FFMPEG_THREADS="${FFMPEG_THREADS:-"0"}"
JOB_TIMEOUT_SECONDS="${JOB_TIMEOUT_SECONDS:-"600"}"

# ── Arg parsing ────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --project) PROJECT_ID="$2"; shift 2 ;;
    --region)  REGION="$2";     shift 2 ;;
    *) echo "Unknown arg: $1"; exit 1 ;;
  esac
done

if [[ -z "$PROJECT_ID" ]]; then
  echo "ERROR: PROJECT_ID is required. Set env var or pass --project."
  exit 1
fi

IMAGE_NAME="gcr.io/${PROJECT_ID}/${SERVICE_NAME}"

echo "==> Building and pushing container image..."
gcloud builds submit \
  --project="${PROJECT_ID}" \
  --tag="${IMAGE_NAME}" \
  .

CLOUD_RUN_URL=$(gcloud run services describe "${SERVICE_NAME}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --format="value(status.url)" 2>/dev/null || echo "")

echo "==> Deploying Cloud Run service: ${SERVICE_NAME}"
gcloud run deploy "${SERVICE_NAME}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --image="${IMAGE_NAME}" \
  \
  `# ── Concurrency: 1 request per instance. CPU-bound ffmpeg work does not` \
  `# benefit from multiple concurrent requests. Cloud Run autoscales new` \
  `# instances horizontally instead.` \
  --concurrency=1 \
  \
  `# ── Resources: 2 vCPUs / 4 GiB gives ffmpeg headroom for the audio` \
  `# transcode. The video is stream-copied so CPU is lightly used. Adjust` \
  `# --cpu and --memory based on profiling.` \
  --cpu=2 \
  --memory=4Gi \
  \
  `# ── Timeout: must exceed JOB_TIMEOUT_SECONDS + gunicorn timeout + margin.` \
  --timeout=720 \
  \
  `# ── Scaling: min 0 (scale to zero when idle), max 20 concurrent instances.` \
  --min-instances=0 \
  --max-instances=20 \
  \
  `# ── Auth: only allow the Pub/Sub invoker SA to call this service.` \
  --no-allow-unauthenticated \
  ${SERVICE_ACCOUNT:+--service-account="${SERVICE_ACCOUNT}"} \
  \
  `# ── Env vars ──────────────────────────────────────────────────────────────` \
  --set-env-vars="GCP_PROJECT_ID=${PROJECT_ID}" \
  --set-env-vars="FIRESTORE_COLLECTION=${FIRESTORE_COLLECTION}" \
  --set-env-vars="AUDIO_BITRATE=${AUDIO_BITRATE}" \
  --set-env-vars="FFMPEG_THREADS=${FFMPEG_THREADS}" \
  --set-env-vars="JOB_TIMEOUT_SECONDS=${JOB_TIMEOUT_SECONDS}" \
  --set-env-vars="OUTPUT_BUCKET=${OUTPUT_BUCKET}" \
  \
  `# PUBSUB_AUDIENCE is set after deploy so we know the service URL` \
  --platform=managed

# Fetch the actual service URL and set PUBSUB_AUDIENCE
CLOUD_RUN_URL=$(gcloud run services describe "${SERVICE_NAME}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --format="value(status.url)")

echo "==> Service URL: ${CLOUD_RUN_URL}"
echo "==> Setting PUBSUB_AUDIENCE env var..."
gcloud run services update "${SERVICE_NAME}" \
  --project="${PROJECT_ID}" \
  --region="${REGION}" \
  --update-env-vars="PUBSUB_AUDIENCE=${CLOUD_RUN_URL}" \
  --platform=managed

# ── Pub/Sub push subscription ─────────────────────────────────────────────────
echo "==> Configure Pub/Sub push subscription manually:"
cat <<EOF

  gcloud pubsub subscriptions create vod-transcode-sub \\
    --project=${PROJECT_ID} \\
    --topic=YOUR_TOPIC_NAME \\
    --push-endpoint=${CLOUD_RUN_URL}/ \\
    --push-auth-service-account=${PUBSUB_SA:-"pubsub-invoker@${PROJECT_ID}.iam.gserviceaccount.com"} \\
    --ack-deadline=660 \\
    --min-retry-delay=10s \\
    --max-retry-delay=300s

EOF

echo "==> Deploy complete."
