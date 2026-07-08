# VOD Platform: Complete Run & Integration Guide

Welcome to your VOD (Video on Demand) platform workspace. This project contains two primary modules:
1. **Transcode-Mux Service (`transcode-service/`)**: A backend processing pipeline to merge separate WAV and H.264 video streams into a web-ready MP4 container optimized for fast progressive seeking.
2. **Universal Shaka Player Wrapper (`vod-player/`)**: A client-side video player abstraction wrapping Google's Shaka Player UI, supporting progressive MP4 range requests today and adaptive DASH/HLS tomorrow with no code changes.

---

## Quick Start: How to Run Locally

### 1. Set Up Environment & Install Dependencies
Run these commands from the root directory (`vod-platform/`):
```bash
# Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies for the transcode service
pip install -r transcode-service/requirements.txt
```

### 2. Run the Offline Transcoder
Transcode local H.264 and WAV files (bypassing GCP/Firestore/GCS):
```bash
cd transcode-service

python3 local_run.py \
  --wav "../test-media/test_audio.wav" \
  --h264 "../test-media/test_video.h264" \
  --output "../test-media/my_output.mp4"
```

### 3. Run the Video Player Test Server
Serve any MP4 with full Range-Request support for Shaka seeking:
```bash
cd ../vod-player

python3 serve.py --media "/path/to/your/video.mp4"
```
* Open `http://localhost:8080/example.html` in your web browser.
* Press `Ctrl + C` in your terminal to shut down the server.

### 4. Run the Production Flask App Locally (Optional)
```bash
cd ../transcode-service

# Setup environment variables (SKIP_AUTH bypasses OIDC validation for local testing)
export GCP_PROJECT_ID="your-gcp-project"
export FIRESTORE_COLLECTION="content"
export SKIP_AUTH="true"

# Start service on http://localhost:8080
python3 main.py
```
To send a mock Pub/Sub request to it:
```bash
PAYLOAD=$(echo '{"content_id":"test-123","wav_gcs_path":"gs://bucket/audio.wav","h264_gcs_path":"gs://bucket/video.h264","output_bucket":"bucket"}' | base64)
curl -X POST http://localhost:8080/ -H "Content-Type: application/json" -d "{\"message\":{\"data\":\"${PAYLOAD}\"}}"
```

---

## Directory Structure

```
vod-platform/
├── README.md                  # Project overview & ABR Migration path
├── RUN_GUIDE.md               # This guide
├── transcode-service/          # Backend transcoding & muxing
│   ├── main.py                # HTTP server (Flask) for Pub/Sub push triggers
│   ├── pipeline.py            # Orchestrator (Download -> Transcode -> Mux -> Probe -> Upload)
│   ├── gcs_client.py          # Streaming-based Google Cloud Storage wrapper
│   ├── metadata_store.py      # Firestore document status & transactional claiming
│   ├── auth.py                # Pub/Sub OIDC Token signature and audience validator
│   ├── logger.py              # Structured JSON logging compatible with Cloud Logging
│   ├── local_run.py           # Standalone local run script (no GCP deps)
│   ├── Dockerfile             # Multi-stage image build with ffmpeg
│   ├── requirements.txt       # Python service dependencies
│   └── deploy.sh              # Cloud Run build and deploy script
└── vod-player/                # Frontend Player Wrapper & Examples
    ├── vod-player.js          # Plain ES Module wrapper for Shaka Player
    ├── VodPlayer.jsx          # React Component wrapper with state overlays
    ├── example.html           # Standalone plain HTML test wrapper page
    ├── cors.json              # GCS Bucket CORS setup for video Range requests
    └── serve.py               # Custom Range-request supported test server
```

---

## 1. Transcode-Mux Service (`transcode-service/`)

### How the Production Pipeline Works
1. **Trigger**: An upload orchestrator publishes a JSON message to a Google Cloud Pub/Sub topic when both WAV and H264 files are present in the GCS bucket.
2. **Push Delivery**: Cloud Run receives a secure HTTP POST request containing the Pub/Sub envelope.
3. **Authentication**: `auth.py` validates the incoming OIDC token (JWT) to ensure only authorized Pub/Sub subscriptions can invoke the endpoint.
4. **Deduplication**: `metadata_store.py` claims the `content_id` using a Firestore Transaction. If a process has already completed or is currently running, the request is safely acknowledged and skipped to prevent duplicate transcoding costs.
5. **Streaming Download**: Files are downloaded directly to temporary storage (`/tmp/` via stream buffers, bypassing high memory allocations).
6. **Processing**:
   - WAV is transcoded into web-standard AAC (`-c:a aac -b:a 128k`).
   - H264 stream-copy is muxed directly into the MP4 container (`-c:v copy`) alongside the newly generated AAC.
   - `-movflags +faststart` is applied to move the `moov` atom (metadata) to the front of the file, allowing fast browser seek requests.
7. **Probing**: `ffprobe` captures duration, codecs, sizes, and resolution.
8. **Firestore Update**: Writes metadata to Firestore under `content/<content_id>`, updating the status to `completed`.
9. **Cleanup**: Context manager ensures local temp files are aggressively removed.

---

### Running the Transcoder Locally

You can test the transcode-mux logic completely offline using local files without any GCP dependencies:

1. **Verify Prerequisites**:
   Ensure you have Python 3 and `ffmpeg`/`ffprobe` installed on your machine.
   ```bash
   which ffmpeg
   which ffprobe
   ```

2. **Run the Transcoder Script**:
   Provide the WAV audio path and the H264 video path:
   ```bash
   cd transcode-service
   python3 local_run.py \
     --wav "../test-media/test_audio.wav" \
     --h264 "../test-media/test_video.h264" \
     --output "../test-media/my_output.mp4" \
     --content-id "my-first-video"
   ```

3. **Check the Output**:
   The script will transcode and output the media metadata to stdout as JSON, and verify that the `moov` atom is ordered correctly for progressive streaming.

---

## 2. Universal Shaka Player Wrapper (`vod-player/`)

The player is designed to play plain progressive MP4 streams today, utilizing native seeking behavior without downloading the whole file. It is built to seamlessly transition to HLS (`.m3u8`) or DASH (`.mpd`) adaptive media formats later without API changes.

### Core Player Capabilities
* **Dynamic CDN Loading**: Automatically checks if `shaka` exists in the global window; if not, it asynchronously pulls Shaka Player UI libraries and styles from Google's hosted CDN.
* **Range Seeking Support**: Recognizes `-movflags +faststart` files, allowing range request playback natively.
* **Error Handling & Signed URLs**: Detects HTTP 403 errors, identifying when a GCS signed URL has expired. It fires `onError` with `err.isExpiredUrl = true` so you can retrieve a fresh signed URL and reload on the fly.
* **UI Customization**: Embeds Shaka UI panel elements (play, seekbar, volume, quality selectors, buffering states) but can be fully bypassed using `enableUi: false` to allow you to build custom HTML UI wrappers.

---

### Standalone HTML Example (`example.html`)
The easiest way to see the player in action is using `example.html`. Because browser security settings restrict loading local scripts/videos via the `file://` protocol, you must run a local server that supports Range Requests.

We have built a dedicated test server `serve.py` for this:

1. **Run the Range Server**:
   Pass the path of the MP4 video file you want to stream.
   ```bash
   cd vod-player
   python3 serve.py --media "/Users/mriduljain/Downloads/Telegram Desktop/7. Real-world Systems/30. Designing Tinder Feed.mp4"
   ```

2. **Open the Player**:
   Open the address provided in your console:
   ```
   http://localhost:8080/example.html
   ```

3. **Test Seeking**:
   Click anywhere on the seekbar. Watch your terminal logs — you will see the server respond with HTTP `206` (Partial Content) Range requests, proving that seeking is instant and only downloads chunks from that timestamp.

---

### React Integration (`VodPlayer.jsx`)
To drop this player into a React 18+ application, copy [VodPlayer.jsx](./vod-player/VodPlayer.jsx) into your components directory.

```jsx
import React, { useState } from 'react';
import VodPlayerComponent from './components/VodPlayer';

export default function VideoSection() {
  const [videoUrl, setVideoUrl] = useState('https://storage.googleapis.com/my-bucket/processed/vid-1/vid-1.mp4');

  const handlePlayerError = (error) => {
    if (error.isExpiredUrl) {
      console.warn("Signed URL expired, fetching a new one...");
      fetch('/api/get-signed-url?id=vid-1')
        .then(res => res.json())
        .then(data => setVideoUrl(data.url)); // Setting state automatically triggers player.load(newSrc)
    } else {
      console.error("Playback error:", error.message);
    }
  };

  return (
    <div style={{ maxWidth: 800, margin: 'auto' }}>
      <VodPlayerComponent
        src={videoUrl}
        enableUi={true}
        onError={handlePlayerError}
        onLoaded={(meta) => console.log('Loaded video dimensions:', meta.videoWidth, meta.videoHeight)}
      />
    </div>
  );
}
```

---

## 3. Production Cloud Deployment Checklist

When deploying the backend transcode service to Google Cloud Platform:

### Step 1: Create a Service Account for Cloud Run
Give the Cloud Run service permissions to read/write buckets and interact with Firestore.
```bash
gcloud iam service-accounts create transcode-runner-sa \
  --description="Accesses GCS & Firestore for transcode service"

# Allow reading raw bucket & writing processed bucket
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member="serviceAccount:transcode-runner-sa@YOUR_PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/storage.objectAdmin"

# Allow reading/writing Firestore
gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member="serviceAccount:transcode-runner-sa@YOUR_PROJECT_ID.iam.gserviceaccount.com" \
  --role="roles/datastore.user"
```

### Step 2: Configure CORS on GCS Processed Bucket
To prevent CORS errors in browsers during seek requests, apply [cors.json](./vod-player/cors.json) to your GCS output bucket. Make sure to update the domains inside the file first.
```bash
gsutil cors set cors.json gs://YOUR_PROCESSED_BUCKET
```

### Step 3: Run Deploy Script
Verify you have Docker or Cloud Build permissions. Set your project env and run:
```bash
cd transcode-service
PROJECT_ID=YOUR_PROJECT_ID ./deploy.sh
```
The script will output the deployed Cloud Run endpoint and generate the `gcloud pubsub subscriptions create` command to bind the Pub/Sub push trigger to your service endpoint.

---

## 4. Scaling to Adaptive Bitrate (ABR) DASH/HLS Later

When your VOD traffic scales and you want to support adaptive streaming (multiple quality levels like 1080p, 720p, 360p adapting to the user's internet speed):

### How the backend changes
1. **Transcoding**: Instead of copy-muxing directly, you will define multiple output renditions. `ffmpeg` will run multi-bitrate encodes (e.g., `-c:v libx264 -b:v 800k` for 480p, `-c:v libx264 -b:v 3000k` for 1080p).
2. **Packaging**: You will run `shaka-packager` on the resulting files to segment them into fragmented MP4 / CMAF formats and output `.mpd` (DASH) and `.m3u8` (HLS) manifests.
3. **Database**: The Firestore `mp4_path` field will store the manifest URL (e.g., `gs://processed-bucket/vid/manifest.mpd`).

### How the frontend changes
* **Zero Code Changes**: The `VodPlayer` wrapper is completely ready. You only update the `src` string from `.../video.mp4` to `.../manifest.mpd`. Shaka Player automatically intercepts this, detects it is a DASH manifest, loads MSE (Media Source Extensions), and handles quality switching on the fly.
