# VOD Transcode-Mux Service: Deployment & Operations Guide

This guide documents the complete architecture, local testing setup, and Google Cloud Platform (GCP) deployment process for the Video-On-Demand (VOD) Transcode-Mux service.

---

## 1. System Architecture

The transcode service is a stateless containerized worker designed to consume split video/audio assets and package them into web-optimized, seekable MP4s.

```
+--------------------+
|  Input GCS Bucket  | <--- Raw audio.wav & video.h264
+--------------------+
          |
          | (Triggered by Upload Event / Admin API)
          v
+--------------------+
|   Pub/Sub Topic    | (vod-upload-ready)
+--------------------+
          |
          | (Push Delivery with Google OIDC JWT Auth)
          v
+--------------------+
| Cloud Run Instance |
|  (ffmpeg worker)   |
+--------------------+
     |    |    |
     |    |    +---> [1] Check Firestore (try_claim dedup locking)
     |    +---------> [2] Download raw inputs, convert WAV -> AAC, stream-copy mux to MP4
     +--------------> [3] Upload processed/movie.mp4 to Output GCS Bucket
```

---

## 2. Infrastructure Inventory (Your Resources)

These are the exact production resources configured for your GCP account:

*   **GCP Project ID:** `project-90479c02-f745-492e-a61`
*   **GCP Region:** `asia-south1` (Mumbai)
*   **Input Storage Bucket:** `gs://project-90479c02-f745-492e-a61-raw-media`
*   **Output Storage Bucket:** `gs://project-90479c02-f745-492e-a61-processed-media`
*   **Firestore Database:** Native Mode (default database)
*   **Cloud Run Service:** `vod-transcode-mux`
*   **Service URL:** `https://vod-transcode-mux-323419402761.asia-south1.run.app`
*   **Pub/Sub Topic:** `vod-upload-ready`
*   **Pub/Sub Subscription:** `vod-transcode-sub`

---

## 3. How to Run Locally

You can run the entire pipeline offline without any GCP dependencies using local inputs.

### Prerequisites
*   Install `ffmpeg` and `ffprobe` locally:
    ```bash
    brew install ffmpeg
    ```

### Running the Local Script
The service includes a `local_run.py` script that bypasses GCS, Pub/Sub, and Firestore, using local files and directories instead.

```bash
# 1. Navigate to the project folder
cd transcode-service/

# 2. Run the local pipeline
python3 local_run.py \
  --wav ./test-media/test_audio.wav \
  --h264 ./test-media/test_video.h264 \
  --output ./test-media/local_result.mp4 \
  --bitrate 128k \
  --content-id "my-local-test"
```

---

## 4. GCP Deployment Checklist (How we deployed it)

Here are the step-by-step commands used to provision the infrastructure and deploy the service.

### Step 4.1: Initializing and Authentication
```bash
# Login to GCP
gcloud auth login

# Set the active project
gcloud config set project project-90479c02-f745-492e-a61
```

### Step 4.2: Enable Services
```bash
gcloud services enable \
  cloudbuild.googleapis.com \
  run.googleapis.com \
  pubsub.googleapis.com \
  firestore.googleapis.com \
  storage.googleapis.com \
  artifactregistry.googleapis.com
```

### Step 4.3: Create GCS Buckets
```bash
# Create raw upload bucket
gcloud storage buckets create gs://project-90479c02-f745-492e-a61-raw-media \
  --location=asia-south1 --uniform-bucket-level-access

# Create processed MP4 bucket
gcloud storage buckets create gs://project-90479c02-f745-492e-a61-processed-media \
  --location=asia-south1 --uniform-bucket-level-access
```

### Step 4.4: Setup Firestore
```bash
gcloud firestore databases create \
  --location=asia-south1 \
  --type=firestore-native
```

### Step 4.5: Setup Service Accounts & IAM Policies
Create the runtime identity for the Cloud Run instance so that it can read raw assets, write processed MP4s, and write metadata records:
```bash
# Create runtime Service Account
gcloud iam service-accounts create vod-transcode-runner \
  --display-name="VOD Transcode Runner"

# Grant roles to the Runtime Service Account
gcloud projects add-iam-policy-binding project-90479c02-f745-492e-a61 \
  --member="serviceAccount:vod-transcode-runner@project-90479c02-f745-492e-a61.iam.gserviceaccount.com" \
  --role="roles/storage.objectViewer"

gcloud projects add-iam-policy-binding project-90479c02-f745-492e-a61 \
  --member="serviceAccount:vod-transcode-runner@project-90479c02-f745-492e-a61.iam.gserviceaccount.com" \
  --role="roles/storage.objectAdmin"

gcloud projects add-iam-policy-binding project-90479c02-f745-492e-a61 \
  --member="serviceAccount:vod-transcode-runner@project-90479c02-f745-492e-a61.iam.gserviceaccount.com" \
  --role="roles/datastore.user"
```

Create the invoker service account for Pub/Sub to trigger the service securely:
```bash
# Create push invoker Service Account
gcloud iam service-accounts create pubsub-invoker \
  --display-name="Pub/Sub Cloud Run Invoker"

# Allow Pub/Sub to generate OIDC Identity Tokens
export PROJECT_NUMBER=$(gcloud projects describe project-90479c02-f745-492e-a61 --format="value(projectNumber)")
gcloud projects add-iam-policy-binding project-90479c02-f745-492e-a61 \
  --member="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com" \
  --role="roles/iam.serviceAccountTokenCreator"
```

### Step 4.6: Set Up Artifact Registry
```bash
gcloud artifacts repositories create vod-images \
  --repository-format=docker \
  --location=asia-south1 \
  --description="VOD service container images"

# Grant writer permission to Cloud Build
gcloud projects add-iam-policy-binding project-90479c02-f745-492e-a61 \
  --member="serviceAccount:${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com" \
  --role="roles/artifactregistry.writer"
```

### Step 4.7: Build & Push Container Image
Run this from inside the `/transcode-service` directory:
```bash
gcloud builds submit \
  --tag="asia-south1-docker.pkg.dev/project-90479c02-f745-492e-a61/vod-images/vod-transcode-mux" .
```

### Step 4.8: Deploy to Cloud Run
```bash
gcloud run deploy vod-transcode-mux \
  --region=asia-south1 \
  --image="asia-south1-docker.pkg.dev/project-90479c02-f745-492e-a61/vod-images/vod-transcode-mux" \
  --service-account="vod-transcode-runner@project-90479c02-f745-492e-a61.iam.gserviceaccount.com" \
  --concurrency=1 \
  --cpu=2 \
  --memory=4Gi \
  --timeout=720 \
  --min-instances=0 \
  --max-instances=5 \
  --no-allow-unauthenticated \
  --set-env-vars="GCP_PROJECT_ID=project-90479c02-f745-492e-a61" \
  --set-env-vars="FIRESTORE_COLLECTION=content" \
  --set-env-vars="AUDIO_BITRATE=128k" \
  --set-env-vars="FFMPEG_THREADS=0" \
  --set-env-vars="JOB_TIMEOUT_SECONDS=600" \
  --set-env-vars="OUTPUT_BUCKET=project-90479c02-f745-492e-a61-processed-media" \
  --platform=managed
```

### Step 4.9: Grant Invocation Rights to Pub/Sub
```bash
gcloud run services add-iam-policy-binding vod-transcode-mux \
  --region=asia-south1 \
  --member="serviceAccount:pubsub-invoker@project-90479c02-f745-492e-a61.iam.gserviceaccount.com" \
  --role="roles/run.invoker"
```

### Step 4.10: Set Token Audience
To protect the endpoint from arbitrary calls, we set the OIDC audience to match the service URL:
```bash
gcloud run services update vod-transcode-mux \
  --region=asia-south1 \
  --update-env-vars="PUBSUB_AUDIENCE=https://vod-transcode-mux-323419402761.asia-south1.run.app" \
  --platform=managed
```

### Step 4.11: Create Pub/Sub Topic and Push Subscription
```bash
# Create Topic
gcloud pubsub topics create vod-upload-ready

# Create Subscription
gcloud pubsub subscriptions create vod-transcode-sub \
  --topic=vod-upload-ready \
  --push-endpoint="https://vod-transcode-mux-323419402761.asia-south1.run.app/" \
  --push-auth-service-account="pubsub-invoker@project-90479c02-f745-492e-a61.iam.gserviceaccount.com" \
  --push-auth-token-audience="https://vod-transcode-mux-323419402761.asia-south1.run.app" \
  --ack-deadline=600 \
  --min-retry-delay=10s \
  --max-retry-delay=300s
```

---

## 5. Web Console UI Deployment (Alternate Method)

If you prefer using the graphical Google Cloud Web Console instead of the `gcloud` command-line tool, follow these steps:

### Step 5.1: Enable APIs
1. Go to **APIs & Services** > **Library** via the top-left navigation menu.
2. Search for the following APIs one by one and click **Enable**:
   *   *Cloud Run API*
   *   *Cloud Build API*
   *   *Cloud Pub/Sub API*
   *   *Google Cloud Storage*
   *   *Google Cloud Firestore API*
   *   *Artifact Registry API*

### Step 5.2: Create Storage Buckets
1. Go to **Cloud Storage** > **Buckets** and click **Create**.
2. Name your bucket (e.g., `project-90479c02-f745-492e-a61-raw-media`), choose region `asia-south1`, set storage class to **Standard**, check **Enforce public access prevention**, select **Uniform** access control, and click **Create**.
3. Repeat the same process to create your output bucket (e.g., `project-90479c02-f745-492e-a61-processed-media`).

### Step 5.3: Set Up Firestore Database
1. Go to **Firestore** in the console.
2. Click **Create Database**.
3. Choose **Firestore in Native Mode** (highly important!).
4. Select location `asia-south1`, leave database ID as `(default)`, and click **Create Database**.

### Step 5.4: Create Service Accounts & Roles
1. Go to **IAM & Admin** > **Service Accounts**.
2. Click **Create Service Account** (Name: `vod-transcode-runner`). Click **Create and Continue**.
3. Under **Grant this service account access to project**, select and assign three roles:
   *   `Storage Object Viewer`
   *   `Storage Object Admin`
   *   `Cloud Datastore User`
4. Click **Done**.
5. Click **Create Service Account** again (Name: `pubsub-invoker`). Click **Done** directly (it needs no direct project roles).
6. To allow Pub/Sub to use this account to call Cloud Run:
   *   Go to **IAM & Admin** > **IAM**.
   *   Check the box **Include Google-provided role grants**.
   *   Find the row for `service-323419402761@gcp-sa-pubsub.iam.gserviceaccount.com` (Pub/Sub Service Agent).
   *   Click the pencil icon to edit it, click **Add Another Role**, search for **Service Account Token Creator**, select it, and click **Save**.

### Step 5.5: Create the Artifact Registry
1. Go to **Artifact Registry** > **Repositories**.
2. Click **Create Repository**.
3. Name it `vod-images`, select format **Docker**, choose location type **Regional** with region `asia-south1`, and click **Create**.

### Step 5.6: Build the Container Image
1. Click the **Activate Cloud Shell** button (looks like `>_` in the top-right header menu).
2. Clone your code repository or upload the files using the Cloud Shell editor.
3. Build the container by pasting this command into the Cloud Shell terminal window:
   ```bash
   gcloud builds submit --project=project-90479c02-f745-492e-a61 --tag="asia-south1-docker.pkg.dev/project-90479c02-f745-492e-a61/vod-images/vod-transcode-mux" .
   ```

### Step 5.7: Deploy to Cloud Run
1. Go to **Cloud Run** and click **Create Service**.
2. Select **Deploy one revision from an existing container image**. Click **Test** or **Browse** to select the image path you created in the step above.
3. Name the service: `vod-transcode-mux`. Select region: `asia-south1`.
4. Under **Authentication**, select **Require authentication** (which secures the service from the public web).
5. Expand the **Container(s), Volumes, Connections, Security** section:
   *   Set CPU to `2` and Memory to `4 GiB`.
   *   Set Container concurrency to `1`.
   *   Set Request timeout to `720` seconds.
   *   Scroll down to **Variables** and add these environment variables:
       *   `GCP_PROJECT_ID` = `project-90479c02-f745-492e-a61`
       *   `FIRESTORE_COLLECTION` = `content`
       *   `AUDIO_BITRATE` = `128k`
       *   `FFMPEG_THREADS` = `0`
       *   `JOB_TIMEOUT_SECONDS` = `600`
       *   `OUTPUT_BUCKET` = `project-90479c02-f745-492e-a61-processed-media`
       *   `PUBSUB_AUDIENCE` = `https://vod-transcode-mux-323419402761.asia-south1.run.app` (Your actual Cloud Run service URL).
   *   Go to **Security** tab, and in the **Service Account** dropdown, select `vod-transcode-runner@project-90479c02-f745-492e-a61.iam.gserviceaccount.com`.
6. Click **Create** to launch.
7. Once created, click the **Security** tab of the service, click **Add Principal**, add `pubsub-invoker@project-90479c02-f745-492e-a61.iam.gserviceaccount.com`, select the role **Cloud Run Invoker**, and click **Save**.

### Step 5.8: Configure Pub/Sub Trigger
1. Go to **Pub/Sub** > **Topics** and click **Create Topic**. Name it `vod-upload-ready` and click **Create**.
2. Go to **Subscriptions** and click **Create Subscription**.
   *   Name: `vod-transcode-sub`
   *   Select Topic: `vod-upload-ready`
   *   Delivery Type: **Push**
   *   Endpoint URL: `https://vod-transcode-mux-323419402761.asia-south1.run.app/`
   *   Check **Enable authentication**:
       *   Select Service Account: `pubsub-invoker@project-90479c02-f745-492e-a61.iam.gserviceaccount.com`
       *   Audience: `https://vod-transcode-mux-323419402761.asia-south1.run.app`
   *   Set **Acknowledgement deadline** to `600` seconds.
3. Click **Create** at the bottom of the page.

---

## 6. How to Run / Test in Production

Here is the operational loop for processing videos in production.

### Step 6.1: Split and Prepare Inputs
If you have `video.mp4`:
```bash
ffmpeg -i video.mp4 -an -vcodec copy video.h264
ffmpeg -i video.mp4 -vn -acodec pcm_s16le audio.wav
```

### Step 6.2: Upload files to GCS
Upload raw splits to the GCS bucket inside a folder named after your content ID:
```bash
export CONTENT_ID="movie-uuid-xyz"

gcloud storage cp audio.wav gs://project-90479c02-f745-492e-a61-raw-media/uploads/${CONTENT_ID}/audio.wav
gcloud storage cp video.h264 gs://project-90479c02-f745-492e-a61-raw-media/uploads/${CONTENT_ID}/video.h264
```

### Step 6.3: Publish Pub/Sub Message (Triggers Pipeline)
Publish the JSON payload which triggers the push delivery:
```bash
gcloud pubsub topics publish vod-upload-ready \
  --message='{
    "content_id": "'"${CONTENT_ID}"'",
    "wav_gcs_path": "gs://project-90479c02-f745-492e-a61-raw-media/uploads/'"${CONTENT_ID}"'/audio.wav",
    "h264_gcs_path": "gs://project-90479c02-f745-492e-a61-raw-media/uploads/'"${CONTENT_ID}"'/video.h264",
    "output_bucket": "project-90479c02-f745-492e-a61-processed-media"
  }'
```

### Step 6.4: Monitor Output & Logs
1.  **Watch execution logs:**
    ```bash
    gcloud run services logs tail vod-transcode-mux --region=asia-south1
    ```
2.  **Verify GCS Output:**
    ```bash
    gcloud storage ls gs://project-90479c02-f745-492e-a61-processed-media/processed/${CONTENT_ID}/
    ```
3.  **Verify database entry:** Visit [Firestore Console](https://console.cloud.google.com/firestore/databases/-default-/data/panel/content?project=project-90479c02-f745-492e-a61) or run:
    ```bash
    gcloud firestore documents describe projects/project-90479c02-f745-492e-a61/databases/\(default\)/documents/content/${CONTENT_ID}
    ```

