"""
VOD Transcode-Mux Service
Cloud Run HTTP service triggered by Pub/Sub push subscription.

Receives a push notification containing content_id, wav_gcs_path,
h264_gcs_path, and output_bucket, then:
  1. Deduplicates using Firestore (content_id + message_id)
  2. Downloads WAV + H264 from GCS (streaming)
  3. Converts WAV → AAC via ffmpeg
  4. Muxes AAC + H264 → MP4 with -movflags +faststart
  5. Runs ffprobe to capture media metadata
  6. Uploads MP4 to GCS (streaming)
  7. Updates Firestore with status + metadata
  8. Cleans up all temp files
"""

import base64
import json
import logging
import os
import sys
import traceback

from flask import Flask, request, jsonify

from auth import verify_pubsub_token
from pipeline import TranscodePipeline
from metadata_store import FirestoreMetadataStore
from logger import setup_structured_logger, log_stage

# ── Logging ────────────────────────────────────────────────────────────────────
setup_structured_logger()
logger = logging.getLogger(__name__)

# ── App ────────────────────────────────────────────────────────────────────────
app = Flask(__name__)

# ── Env config ─────────────────────────────────────────────────────────────────
EXPECTED_AUDIENCE = os.environ.get("PUBSUB_AUDIENCE", "")  # Cloud Run service URL
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "")
FIRESTORE_COLLECTION = os.environ.get("FIRESTORE_COLLECTION", "content")
SKIP_AUTH = os.environ.get("SKIP_AUTH", "false").lower() == "true"  # local dev only


@app.route("/", methods=["POST"])
def handle_pubsub_push():
    """
    Entry point for Pub/Sub push messages.
    Returns:
      204 — success / permanent failure (both acked)
      500 — transient failure (nacked, Pub/Sub will retry)
    """
    envelope = request.get_json(silent=True)
    if not envelope:
        logger.error("Bad request: empty or non-JSON body")
        return jsonify({"error": "Bad Request"}), 400

    # ── Auth ───────────────────────────────────────────────────────────────────
    if not SKIP_AUTH:
        auth_header = request.headers.get("Authorization", "")
        if not verify_pubsub_token(auth_header, EXPECTED_AUDIENCE):
            logger.warning("Unauthorized push request rejected")
            return jsonify({"error": "Unauthorized"}), 401

    # ── Decode Pub/Sub envelope ────────────────────────────────────────────────
    try:
        message = envelope.get("message", {})
        message_id = message.get("messageId", "unknown")
        raw_data = message.get("data", "")
        payload = json.loads(base64.b64decode(raw_data).decode("utf-8"))
    except Exception as exc:
        # Permanently malformed — ack so Pub/Sub doesn't retry forever
        logger.error("Failed to decode Pub/Sub message: %s", exc)
        return "", 204

    content_id = payload.get("content_id")
    wav_path = payload.get("wav_gcs_path")
    h264_path = payload.get("h264_gcs_path")
    output_bucket = payload.get("output_bucket")

    if not all([content_id, wav_path, h264_path, output_bucket]):
        logger.error(
            "Permanently invalid payload — missing required fields",
            extra={"payload": payload, "message_id": message_id},
        )
        return "", 204  # ack — retrying won't fix a malformed payload

    log_stage(logger, content_id, "received", "info", message_id=message_id)

    # ── Pipeline ───────────────────────────────────────────────────────────────
    store = FirestoreMetadataStore(GCP_PROJECT_ID, FIRESTORE_COLLECTION)
    pipeline = TranscodePipeline(store)

    try:
        result = pipeline.run(
            content_id=content_id,
            message_id=message_id,
            wav_gcs_path=wav_path,
            h264_gcs_path=h264_path,
            output_bucket=output_bucket,
        )
    except pipeline.TransientError as exc:
        # Transient (network, GCS timeout, etc.) — nack so Pub/Sub retries
        logger.error(
            "Transient error — nacking for retry: %s",
            exc,
            extra={"content_id": content_id},
        )
        return jsonify({"error": str(exc)}), 500
    except Exception as exc:
        # Unexpected permanent error — ack to avoid infinite retry
        logger.error(
            "Unexpected permanent error — acking: %s\n%s",
            exc,
            traceback.format_exc(),
            extra={"content_id": content_id},
        )
        return "", 204

    if result == "skipped":
        log_stage(logger, content_id, "deduplicated", "info", message_id=message_id)

    return "", 204  # ack


@app.route("/healthz", methods=["GET"])
def healthz():
    return jsonify({"status": "ok"}), 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
