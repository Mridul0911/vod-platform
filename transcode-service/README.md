# VOD Transcode-Mux Service

A Cloud Run HTTP service that consumes Pub/Sub push notifications and converts
raw WAV + H264 inputs into web-ready MP4 files.

---

## Architecture

```
GCS (raw)
  └─ wav + h264 ──► Pub/Sub topic ──► Push subscription ──► Cloud Run
                                                               │
                                      ┌────────────────────────┘
                                      │  1. Download (streaming)
                                      │  2. WAV → AAC (ffmpeg)
                                      │  3. AAC + H264 → MP4 (stream-copy, faststart)
                                      │  4. ffprobe (media metadata)
                                      │  5. Upload MP4 (streaming)
                                      │  6. Firestore update
                                      └─► GCS (processed) + Firestore
```

---

## Environment Variables

| Variable               | Required | Default     | Description                                                  |
|------------------------|----------|-------------|--------------------------------------------------------------|
| `GCP_PROJECT_ID`       | ✅        | —           | GCP project for Firestore client                             |
| `PUBSUB_AUDIENCE`      | ✅        | —           | Cloud Run service URL — used to verify Pub/Sub OIDC tokens   |
| `FIRESTORE_COLLECTION` | ❌        | `content`   | Firestore collection name for content metadata               |
| `AUDIO_BITRATE`        | ❌        | `128k`      | ffmpeg `-b:a` bitrate for AAC output                         |
| `FFMPEG_THREADS`       | ❌        | `0`         | ffmpeg `-threads` (0 = auto-detect from available CPUs)      |
| `JOB_TIMEOUT_SECONDS`  | ❌        | `600`       | Max seconds allowed for a single ffmpeg call (10 min)        |
| `GCS_CHUNK_SIZE_BYTES` | ❌        | `8388608`   | GCS streaming chunk size (8 MiB default)                     |
| `SKIP_AUTH`            | ❌        | `false`     | Set `true` only in local dev to bypass OIDC verification     |
| `OUTPUT_BUCKET`        | ❌        | (in payload)| Can override the output bucket from env                      |
| `FFMPEG_BIN`           | ❌        | `ffmpeg`    | Path to ffmpeg binary (if not on PATH)                       |
| `FFPROBE_BIN`          | ❌        | `ffprobe`   | Path to ffprobe binary (if not on PATH)                      |

---

## Pub/Sub Message Payload

The push subscription delivers a base64-encoded JSON body:

```json
{
  "content_id":    "movie-abc-123",
  "wav_gcs_path":  "gs://raw-media-bucket/uploads/movie-abc-123/audio.wav",
  "h264_gcs_path": "gs://raw-media-bucket/uploads/movie-abc-123/video.h264",
  "output_bucket": "processed-media-bucket"
}
```

### Output GCS Path

The service writes to a deterministic path:

```
gs://<output_bucket>/processed/<content_id>/<content_id>.mp4
```

---

## Pub/Sub Subscription Setup

```bash
# 1. Create a service account for Pub/Sub to use when invoking Cloud Run
gcloud iam service-accounts create pubsub-invoker \
  --display-name="Pub/Sub Cloud Run Invoker"

# 2. Grant it Cloud Run Invoker role on the service
gcloud run services add-iam-policy-binding vod-transcode-mux \
  --region=us-central1 \
  --member="serviceAccount:pubsub-invoker@PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/run.invoker"

# 3. Grant Pub/Sub permission to create tokens as this SA
gcloud projects add-iam-policy-binding PROJECT_ID \
  --member="serviceAccount:service-PROJECT_NUMBER@gcp-sa-pubsub.iam.gserviceaccount.com" \
  --role="roles/iam.serviceAccountTokenCreator"

# 4. Create the push subscription
gcloud pubsub subscriptions create vod-transcode-sub \
  --topic=vod-upload-ready \
  --push-endpoint=https://YOUR_SERVICE_URL/ \
  --push-auth-service-account=pubsub-invoker@PROJECT_ID.iam.gserviceaccount.com \
  --ack-deadline=660 \
  --min-retry-delay=10s \
  --max-retry-delay=300s
```

---

## Firestore Schema

Collection: `content` (configurable via `FIRESTORE_COLLECTION`)  
Document ID: `content_id`

```jsonc
{
  "content_id":       "movie-abc-123",
  "status":           "processing" | "completed" | "failed",
  "message_id":       "pub-sub-msg-id",          // dedup key
  "mp4_path":         "gs://bucket/processed/...",
  "duration_seconds": 5432.1,
  "resolution":       "1920x1080",
  "width":            1920,
  "height":           1080,
  "video_codec":      "h264",
  "audio_codec":      "aac",
  "bitrate_kbps":     3200,
  "file_size_bytes":  2147483648,
  "error_message":    null,                        // set on failure
  "created_at":       "2026-01-01T00:00:00Z",
  "updated_at":       "2026-01-01T00:05:00Z",
  "completed_at":     "2026-01-01T00:05:00Z"
}
```

### Recommended Firestore Indexes

```
Collection: content
Composite index: status ASC, created_at DESC   (for ops dashboards querying by status)
```

---

## IAM Requirements

The Cloud Run service's runtime service account needs:

| Resource                     | Role                              |
|------------------------------|-----------------------------------|
| Input GCS bucket             | `roles/storage.objectViewer`      |
| Output GCS bucket            | `roles/storage.objectAdmin`       |
| Firestore database           | `roles/datastore.user`            |

---

## GCS Object Lifecycle Recommendations

After a successful mux, the raw source files (WAV + H264) are no longer
needed for processing. Configure lifecycle rules on the **input/raw bucket**:

### Transition to Nearline (cheaper storage) after 7 days
```json
{
  "rule": [{
    "action": { "type": "SetStorageClass", "storageClass": "NEARLINE" },
    "condition": { "age": 7 }
  }]
}
```

### Or delete raw inputs after 30 days (if content_id status=completed)
```json
{
  "rule": [{
    "action": { "type": "Delete" },
    "condition": { "age": 30 }
  }]
}
```

Apply with:
```bash
gsutil lifecycle set lifecycle.json gs://YOUR_RAW_BUCKET
```

> **Note:** GCS lifecycle rules operate on object age, not Firestore status.
> If you need conditional cleanup (only delete if mux succeeded), implement
> a separate Cloud Function triggered by Firestore writes that deletes the
> source files once `status == "completed"`.

---

## Ack / Nack Semantics

| Scenario                       | HTTP Response | Pub/Sub Action        |
|--------------------------------|---------------|-----------------------|
| Success                        | 204           | Acked                 |
| Already completed (dedup)      | 204           | Acked                 |
| Malformed/invalid payload      | 204           | Acked (won't fix)     |
| Corrupt input (ffmpeg error)   | 204           | Acked (won't fix)     |
| GCS timeout / network error    | 500           | Nacked → retry        |
| ffmpeg timeout                 | 500           | Nacked → retry        |

---

## Local Development

```bash
# Install ffmpeg
brew install ffmpeg   # macOS

# Set up Python env
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Export required env vars
export GCP_PROJECT_ID=my-project
export SKIP_AUTH=true
export OUTPUT_BUCKET=my-output-bucket

# Run locally
python main.py

# Send a test push (base64-encode the payload)
PAYLOAD=$(echo '{"content_id":"test-001","wav_gcs_path":"gs://bucket/test.wav","h264_gcs_path":"gs://bucket/test.h264","output_bucket":"my-bucket"}' | base64)
curl -X POST http://localhost:8080/ \
  -H "Content-Type: application/json" \
  -d "{\"message\":{\"messageId\":\"test-msg-1\",\"data\":\"${PAYLOAD}\"}}"
```
