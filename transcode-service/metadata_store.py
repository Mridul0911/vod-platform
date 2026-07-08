"""
metadata_store.py — Firestore-backed metadata store for content processing state.

Collection: 'content' (configurable via FIRESTORE_COLLECTION env var)
Document ID: content_id

Schema:
  {
    "content_id":       string,
    "status":           "processing" | "completed" | "failed",
    "message_id":       string,       // Pub/Sub message ID (for dedup)
    "mp4_path":         string,       // gs://bucket/path/to/output.mp4
    "duration_seconds": float,
    "resolution":       string,       // e.g. "1920x1080"
    "width":            int,
    "height":           int,
    "video_codec":      string,
    "audio_codec":      string,
    "bitrate_kbps":     int,
    "file_size_bytes":  int,
    "error_message":    string | null,
    "created_at":       timestamp,
    "updated_at":       timestamp,
  }
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from google.cloud import firestore

logger = logging.getLogger(__name__)


class FirestoreMetadataStore:
    """Manages content processing state in Firestore with optimistic dedup locking."""

    def __init__(self, project_id: str, collection: str = "content"):
        self._db = firestore.Client(project=project_id)
        self._collection = collection

    def _doc_ref(self, content_id: str):
        return self._db.collection(self._collection).document(content_id)

    def try_claim(self, content_id: str, message_id: str) -> bool:
        """
        Attempt to claim a content_id for processing using a Firestore transaction.

        Returns True if the claim was successful (this instance should proceed).
        Returns False if the document already exists with status != 'failed'
        (meaning another instance has claimed it or it's already done).

        A 'failed' job is allowed to be re-claimed so it can be retried on
        the next Pub/Sub delivery attempt.
        """
        doc_ref = self._doc_ref(content_id)

        @firestore.transactional
        def _claim_in_txn(txn):
            snapshot = doc_ref.get(transaction=txn)
            if snapshot.exists:
                current_status = snapshot.get("status")
                current_msg_id = snapshot.get("message_id")

                # Already completed — skip
                if current_status == "completed":
                    logger.info(
                        "content_id=%s already completed — dedup skip", content_id
                    )
                    return False

                # In-progress from a different message — skip (avoid race)
                if current_status == "processing" and current_msg_id != message_id:
                    logger.info(
                        "content_id=%s already processing (msg=%s) — skip",
                        content_id, current_msg_id,
                    )
                    return False

                # Same message replayed (at-least-once) — allow re-entry
                if current_msg_id == message_id:
                    logger.info(
                        "content_id=%s replayed message_id=%s — allowing re-entry",
                        content_id, message_id,
                    )
                    return True  # idempotent re-run of same message

            # New or previously failed — claim it
            now = datetime.now(timezone.utc)
            txn.set(doc_ref, {
                "content_id": content_id,
                "message_id": message_id,
                "status": "processing",
                "created_at": now,
                "updated_at": now,
                "error_message": None,
            })
            return True

        txn = self._db.transaction()
        return _claim_in_txn(txn)

    def set_status(
        self,
        content_id: str,
        status: str,
        error_message: Optional[str] = None,
    ):
        """Update only the status (and error_message) fields."""
        update: dict = {
            "status": status,
            "updated_at": datetime.now(timezone.utc),
        }
        if error_message is not None:
            update["error_message"] = error_message
        self._doc_ref(content_id).update(update)
        logger.info("Firestore status update: content_id=%s status=%s", content_id, status)

    def set_completed(
        self,
        content_id: str,
        mp4_path: str,
        media_info: dict,
    ):
        """
        Mark a job as completed and store all media metadata captured from ffprobe.
        """
        now = datetime.now(timezone.utc)
        update = {
            "status": "completed",
            "mp4_path": mp4_path,
            "updated_at": now,
            "completed_at": now,
            "error_message": None,
            **media_info,  # duration_seconds, resolution, codecs, bitrate, file_size_bytes
        }
        self._doc_ref(content_id).update(update)
        logger.info(
            "Firestore completed: content_id=%s mp4_path=%s", content_id, mp4_path
        )
