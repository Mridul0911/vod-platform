"""
gcs_client.py — Streaming GCS download and upload helpers.

Uses google-cloud-storage's blob.download_to_file() and
blob.upload_from_file() which internally use resumable / chunked transfers.
Neither function loads the entire file into memory.
"""

import logging
import os
from pathlib import Path

from google.cloud import storage

logger = logging.getLogger(__name__)

# Chunk size for streaming transfers — 8 MiB is the GCS client default
_CHUNK_SIZE = int(os.environ.get("GCS_CHUNK_SIZE_BYTES", 8 * 1024 * 1024))


def _parse_gcs_path(gcs_path: str) -> tuple[str, str]:
    """
    Parse 'gs://bucket/path/to/object' → ('bucket', 'path/to/object').
    Also accepts 'bucket/path/to/object' (no scheme).
    """
    if gcs_path.startswith("gs://"):
        gcs_path = gcs_path[5:]
    parts = gcs_path.split("/", 1)
    if len(parts) != 2:
        raise ValueError(f"Invalid GCS path: {gcs_path!r}")
    return parts[0], parts[1]


class GCSClient:
    """Thin wrapper around google-cloud-storage for streaming I/O."""

    def __init__(self):
        self._client = storage.Client()

    def download_streaming(self, gcs_path: str, local_path: Path):
        """
        Stream-download a GCS object to local_path without buffering the whole
        file in memory. Uses blob.download_to_file() which issues Range requests
        under the hood.
        """
        bucket_name, blob_name = _parse_gcs_path(gcs_path)
        bucket = self._client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        blob.chunk_size = _CHUNK_SIZE

        logger.info(
            "Downloading gs://%s/%s → %s", bucket_name, blob_name, local_path
        )
        with open(local_path, "wb") as fh:
            blob.download_to_file(fh)
        logger.info("Download complete: %s (%d bytes)", local_path, local_path.stat().st_size)

    def upload_streaming(self, local_path: Path, bucket_name: str, blob_name: str):
        """
        Stream-upload local_path to GCS using a resumable upload session.
        The blob is written in chunks so the whole MP4 never lives in RAM.
        """
        bucket = self._client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        blob.chunk_size = _CHUNK_SIZE

        logger.info(
            "Uploading %s → gs://%s/%s", local_path, bucket_name, blob_name
        )
        with open(local_path, "rb") as fh:
            blob.upload_from_file(
                fh,
                content_type="video/mp4",
                rewind=True,  # ensures file pointer is at start even if reused
            )
        logger.info(
            "Upload complete: gs://%s/%s", bucket_name, blob_name
        )
