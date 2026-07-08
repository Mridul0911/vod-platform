"""
pipeline.py — Core transcode-mux pipeline logic.

Stages:
  1. Deduplication check via Firestore
  2. Streaming download of WAV + H264 from GCS
  3. WAV → AAC transcode (ffmpeg)
  4. AAC + H264 → MP4 mux with -movflags +faststart (ffmpeg stream-copy)
  5. ffprobe: capture duration, resolution, codecs, bitrate, file size
  6. Streaming upload of MP4 to GCS
  7. Firestore metadata update (status, mp4_path, media info)
  8. Temp file cleanup (try/finally — always runs)
"""

import logging
import os
import subprocess
import tempfile
import time
import json
from pathlib import Path
from typing import Optional

from gcs_client import GCSClient
from metadata_store import FirestoreMetadataStore
from logger import log_stage

logger = logging.getLogger(__name__)

# ── Env config ─────────────────────────────────────────────────────────────────
AUDIO_BITRATE = os.environ.get("AUDIO_BITRATE", "128k")
FFMPEG_THREADS = os.environ.get("FFMPEG_THREADS", "0")   # 0 = let ffmpeg auto-detect
JOB_TIMEOUT_SECONDS = int(os.environ.get("JOB_TIMEOUT_SECONDS", "600"))  # 10 min default
FFMPEG_BIN = os.environ.get("FFMPEG_BIN", "ffmpeg")
FFPROBE_BIN = os.environ.get("FFPROBE_BIN", "ffprobe")


class TranscodePipeline:
    """Orchestrates the full download → transcode → mux → upload flow."""

    class TransientError(Exception):
        """Raised for retriable failures (network timeouts, GCS errors, etc.)"""

    class PermanentError(Exception):
        """Raised for non-retriable failures (corrupt input, ffmpeg hard error)."""

    def __init__(self, metadata_store: FirestoreMetadataStore):
        self._store = metadata_store
        self._gcs = GCSClient()

    # ── Public entry ───────────────────────────────────────────────────────────

    def run(
        self,
        content_id: str,
        message_id: str,
        wav_gcs_path: str,
        h264_gcs_path: str,
        output_bucket: str,
    ) -> str:
        """
        Run the full pipeline for a given content_id.

        Returns:
          "skipped"   — already processed / in-progress (dedup)
          "completed" — successfully muxed and uploaded
        Raises:
          TransientError — caller should nack
          PermanentError — caller should ack (no point retrying)
        """
        # ── 1. Deduplication ───────────────────────────────────────────────────
        if not self._store.try_claim(content_id, message_id):
            logger.info(
                "Job already claimed for content_id=%s — skipping", content_id
            )
            return "skipped"

        # Mark as processing immediately so concurrent duplicates are blocked
        self._store.set_status(content_id, "processing")

        with tempfile.TemporaryDirectory(prefix=f"vod_{content_id}_") as tmpdir:
            tmp = Path(tmpdir)
            wav_local = tmp / "input.wav"
            h264_local = tmp / "input.h264"
            aac_local = tmp / "audio.aac"
            mp4_local = tmp / "output.mp4"
            output_gcs_path = f"processed/{content_id}/{content_id}.mp4"

            try:
                self._run_pipeline(
                    content_id=content_id,
                    wav_gcs_path=wav_gcs_path,
                    h264_gcs_path=h264_gcs_path,
                    wav_local=wav_local,
                    h264_local=h264_local,
                    aac_local=aac_local,
                    mp4_local=mp4_local,
                    output_bucket=output_bucket,
                    output_gcs_path=output_gcs_path,
                )
            except (self.TransientError, self.PermanentError) as exc:
                error_msg = str(exc)
                self._store.set_status(content_id, "failed", error_message=error_msg)
                log_stage(logger, content_id, "failed", "error", error=error_msg)
                raise  # re-raise so caller can decide ack/nack
            except Exception as exc:
                error_msg = f"Unexpected: {exc}"
                self._store.set_status(content_id, "failed", error_message=error_msg)
                log_stage(logger, content_id, "failed", "error", error=error_msg)
                raise self.PermanentError(error_msg) from exc
            # tmpdir and all files inside are removed here by TemporaryDirectory

        return "completed"

    # ── Internal pipeline stages ───────────────────────────────────────────────

    def _run_pipeline(
        self,
        content_id: str,
        wav_gcs_path: str,
        h264_gcs_path: str,
        wav_local: Path,
        h264_local: Path,
        aac_local: Path,
        mp4_local: Path,
        output_bucket: str,
        output_gcs_path: str,
    ):
        # ── Stage 2: Download ──────────────────────────────────────────────────
        t0 = time.monotonic()
        log_stage(logger, content_id, "download_start", "info")
        try:
            self._gcs.download_streaming(wav_gcs_path, wav_local)
            self._gcs.download_streaming(h264_gcs_path, h264_local)
        except Exception as exc:
            raise self.TransientError(f"GCS download failed: {exc}") from exc

        log_stage(
            logger, content_id, "download_complete", "info",
            duration_ms=int((time.monotonic() - t0) * 1000),
        )

        # ── Stage 3: WAV → AAC ────────────────────────────────────────────────
        t0 = time.monotonic()
        log_stage(logger, content_id, "audio_transcode_start", "info")
        self._transcode_wav_to_aac(wav_local, aac_local, content_id)
        log_stage(
            logger, content_id, "audio_transcode_complete", "info",
            duration_ms=int((time.monotonic() - t0) * 1000),
        )

        # ── Stage 4: Mux AAC + H264 → MP4 ────────────────────────────────────
        t0 = time.monotonic()
        log_stage(logger, content_id, "mux_start", "info")
        self._mux_to_mp4(h264_local, aac_local, mp4_local, content_id)
        log_stage(
            logger, content_id, "mux_complete", "info",
            duration_ms=int((time.monotonic() - t0) * 1000),
        )

        # ── Stage 5: ffprobe ──────────────────────────────────────────────────
        log_stage(logger, content_id, "probe_start", "info")
        media_info = self._probe_mp4(mp4_local, content_id)
        log_stage(logger, content_id, "probe_complete", "info", **media_info)

        # ── Stage 6: Upload ───────────────────────────────────────────────────
        t0 = time.monotonic()
        log_stage(logger, content_id, "upload_start", "info")
        try:
            self._gcs.upload_streaming(mp4_local, output_bucket, output_gcs_path)
        except Exception as exc:
            raise self.TransientError(f"GCS upload failed: {exc}") from exc

        log_stage(
            logger, content_id, "upload_complete", "info",
            duration_ms=int((time.monotonic() - t0) * 1000),
        )

        # ── Stage 7: Firestore update ──────────────────────────────────────────
        self._store.set_completed(
            content_id=content_id,
            mp4_path=f"gs://{output_bucket}/{output_gcs_path}",
            media_info=media_info,
        )
        log_stage(logger, content_id, "completed", "info", mp4_path=output_gcs_path)

    # ── ffmpeg helpers ─────────────────────────────────────────────────────────

    def _run_ffmpeg(self, args: list, content_id: str, stage: str):
        """
        Run an ffmpeg command with a configurable timeout.
        Raises PermanentError on non-zero exit, TransientError on timeout.
        """
        cmd = [FFMPEG_BIN, "-threads", str(FFMPEG_THREADS)] + args
        logger.debug("Running ffmpeg: %s", " ".join(cmd))
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=JOB_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            raise self.TransientError(
                f"ffmpeg timed out after {JOB_TIMEOUT_SECONDS}s at stage={stage}"
            )

        if result.returncode != 0:
            stderr_tail = result.stderr[-2000:] if result.stderr else ""
            raise self.PermanentError(
                f"ffmpeg failed (stage={stage}, rc={result.returncode}): {stderr_tail}"
            )

    def _transcode_wav_to_aac(self, wav_local: Path, aac_local: Path, content_id: str):
        """WAV → AAC using native aac encoder."""
        self._run_ffmpeg(
            [
                "-y",
                "-i", str(wav_local),
                "-c:a", "aac",
                "-b:a", AUDIO_BITRATE,
                str(aac_local),
            ],
            content_id=content_id,
            stage="wav_to_aac",
        )

    def _mux_to_mp4(
        self,
        h264_local: Path,
        aac_local: Path,
        mp4_local: Path,
        content_id: str,
    ):
        """
        Mux H264 (stream-copy) + AAC into MP4 with faststart.
        -movflags +faststart moves the moov atom to the front, enabling
        Range-request based seeking on progressive MP4 without a manifest.
        """
        self._run_ffmpeg(
            [
                "-y",
                "-i", str(h264_local),
                "-i", str(aac_local),
                "-c:v", "copy",       # stream-copy — no re-encode
                "-c:a", "copy",       # AAC already encoded above
                "-movflags", "+faststart",
                str(mp4_local),
            ],
            content_id=content_id,
            stage="mux_mp4",
        )

    def _probe_mp4(self, mp4_local: Path, content_id: str) -> dict:
        """
        Run ffprobe on the output MP4 and return a dict of media metadata.
        Raises PermanentError if ffprobe fails.
        """
        cmd = [
            FFPROBE_BIN,
            "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            "-show_format",
            str(mp4_local),
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            raise self.TransientError("ffprobe timed out")

        if result.returncode != 0:
            raise self.PermanentError(f"ffprobe failed: {result.stderr[:500]}")

        probe_data = json.loads(result.stdout)
        fmt = probe_data.get("format", {})
        streams = probe_data.get("streams", [])

        video_stream = next(
            (s for s in streams if s.get("codec_type") == "video"), {}
        )
        audio_stream = next(
            (s for s in streams if s.get("codec_type") == "audio"), {}
        )

        width = video_stream.get("width")
        height = video_stream.get("height")

        return {
            "duration_seconds": float(fmt.get("duration", 0)),
            "resolution": f"{width}x{height}" if width and height else "unknown",
            "width": width,
            "height": height,
            "video_codec": video_stream.get("codec_name", "unknown"),
            "audio_codec": audio_stream.get("codec_name", "unknown"),
            "bitrate_kbps": int(int(fmt.get("bit_rate", 0)) / 1000),
            "file_size_bytes": int(fmt.get("size", 0)),
        }
