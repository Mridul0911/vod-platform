#!/usr/bin/env python3
"""
local_run.py — Run the transcode-mux pipeline locally with plain file paths.

Bypasses GCS, Pub/Sub, Firestore, and auth entirely. Uses local file copy
instead of streaming GCS downloads, and prints metadata to stdout instead
of writing to Firestore.

Usage:
  python local_run.py --wav /path/to/audio.wav --h264 /path/to/video.h264

  # With custom output path:
  python local_run.py --wav audio.wav --h264 video.h264 --output ./my_output.mp4

  # With custom audio bitrate:
  python local_run.py --wav audio.wav --h264 video.h264 --bitrate 192k

Requirements:
  - ffmpeg and ffprobe installed and on PATH (brew install ffmpeg)
  - No GCP dependencies — runs fully offline
"""

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ── Env-configurable options (or override via CLI) ─────────────────────────────
AUDIO_BITRATE = os.environ.get("AUDIO_BITRATE", "128k")
FFMPEG_THREADS = os.environ.get("FFMPEG_THREADS", "0")
JOB_TIMEOUT_SECONDS = int(os.environ.get("JOB_TIMEOUT_SECONDS", "600"))
FFMPEG_BIN = os.environ.get("FFMPEG_BIN", "ffmpeg")
FFPROBE_BIN = os.environ.get("FFPROBE_BIN", "ffprobe")


# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("local_run")


# ── Helpers ────────────────────────────────────────────────────────────────────

def check_prerequisites():
    """Verify that ffmpeg and ffprobe are installed."""
    for binary in [FFMPEG_BIN, FFPROBE_BIN]:
        try:
            subprocess.run(
                [binary, "-version"],
                capture_output=True,
                timeout=10,
            )
        except FileNotFoundError:
            logger.error(
                "❌ '%s' not found. Install ffmpeg first:\n"
                "   brew install ffmpeg      (macOS)\n"
                "   apt install ffmpeg       (Ubuntu/Debian)",
                binary,
            )
            sys.exit(1)
    logger.info("✅ ffmpeg and ffprobe found")


def validate_input(path: Path, label: str):
    """Check that the input file exists and is non-empty."""
    if not path.exists():
        logger.error("❌ %s file not found: %s", label, path)
        sys.exit(1)
    if path.stat().st_size == 0:
        logger.error("❌ %s file is empty (0 bytes): %s", label, path)
        sys.exit(1)
    size_mb = path.stat().st_size / (1024 * 1024)
    logger.info("✅ %s: %s (%.1f MB)", label, path, size_mb)


def run_ffmpeg(args: list, stage: str):
    """Run an ffmpeg command. Raises SystemExit on failure."""
    cmd = [FFMPEG_BIN, "-threads", str(FFMPEG_THREADS)] + args
    logger.info("⚙️  Running: %s", " ".join(cmd))
    t0 = time.monotonic()

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=JOB_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        logger.error("❌ ffmpeg timed out after %ds at stage=%s", JOB_TIMEOUT_SECONDS, stage)
        sys.exit(1)

    elapsed = time.monotonic() - t0

    if result.returncode != 0:
        logger.error(
            "❌ ffmpeg failed at stage=%s (exit code %d)\n\nSTDERR:\n%s",
            stage,
            result.returncode,
            result.stderr[-3000:] if result.stderr else "(empty)",
        )
        sys.exit(1)

    logger.info("✅ %s completed in %.1fs", stage, elapsed)


def transcode_wav_to_aac(wav_path: Path, aac_path: Path, bitrate: str):
    """WAV → AAC."""
    run_ffmpeg(
        [
            "-y",
            "-i", str(wav_path),
            "-c:a", "aac",
            "-b:a", bitrate,
            str(aac_path),
        ],
        stage="WAV → AAC",
    )


def mux_to_mp4(h264_path: Path, aac_path: Path, mp4_path: Path):
    """Mux H264 (stream-copy) + AAC → MP4 with -movflags +faststart."""
    run_ffmpeg(
        [
            "-y",
            "-i", str(h264_path),
            "-i", str(aac_path),
            "-c:v", "copy",
            "-c:a", "copy",
            "-movflags", "+faststart",
            str(mp4_path),
        ],
        stage="Mux → MP4",
    )


def probe_mp4(mp4_path: Path) -> dict:
    """Run ffprobe and return media metadata as a dict."""
    cmd = [
        FFPROBE_BIN,
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        str(mp4_path),
    ]
    logger.info("⚙️  Running: %s", " ".join(cmd))

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        logger.error("❌ ffprobe timed out")
        sys.exit(1)

    if result.returncode != 0:
        logger.error("❌ ffprobe failed: %s", result.stderr[:500])
        sys.exit(1)

    probe_data = json.loads(result.stdout)
    fmt = probe_data.get("format", {})
    streams = probe_data.get("streams", [])

    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})

    width = video.get("width")
    height = video.get("height")

    return {
        "duration_seconds": float(fmt.get("duration", 0)),
        "resolution": f"{width}x{height}" if width and height else "unknown",
        "width": width,
        "height": height,
        "video_codec": video.get("codec_name", "unknown"),
        "audio_codec": audio.get("codec_name", "unknown"),
        "bitrate_kbps": int(int(fmt.get("bit_rate", 0)) / 1000),
        "file_size_bytes": int(fmt.get("size", 0)),
    }


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Local VOD transcode-mux pipeline. No GCP deps required.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python local_run.py --wav audio.wav --h264 video.h264
  python local_run.py --wav audio.wav --h264 video.h264 --output result.mp4
  python local_run.py --wav audio.wav --h264 video.h264 --bitrate 192k
        """,
    )
    parser.add_argument("--wav", required=True, help="Path to WAV audio file")
    parser.add_argument("--h264", required=True, help="Path to H264 elementary stream file")
    parser.add_argument("--output", "-o", default=None, help="Output MP4 path (default: <wav_dir>/output.mp4)")
    parser.add_argument("--bitrate", "-b", default=AUDIO_BITRATE, help=f"AAC audio bitrate (default: {AUDIO_BITRATE})")
    parser.add_argument("--content-id", default="local-test", help="Content ID for logging (default: local-test)")

    args = parser.parse_args()

    wav_path = Path(args.wav).resolve()
    h264_path = Path(args.h264).resolve()
    content_id = args.content_id

    # Default output beside the WAV file
    if args.output:
        output_path = Path(args.output).resolve()
    else:
        output_path = wav_path.parent / f"{content_id}_output.mp4"

    bitrate = args.bitrate

    # ── Preflight ──────────────────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("VOD Transcode-Mux — Local Runner")
    logger.info("=" * 60)
    logger.info("Content ID : %s", content_id)
    logger.info("WAV input  : %s", wav_path)
    logger.info("H264 input : %s", h264_path)
    logger.info("MP4 output : %s", output_path)
    logger.info("AAC bitrate: %s", bitrate)
    logger.info("")

    check_prerequisites()
    validate_input(wav_path, "WAV")
    validate_input(h264_path, "H264")

    # ── Pipeline (uses temp dir for intermediates, output goes to final path) ──
    total_t0 = time.monotonic()

    with tempfile.TemporaryDirectory(prefix=f"vod_{content_id}_") as tmpdir:
        tmp = Path(tmpdir)
        aac_tmp = tmp / "audio.aac"
        mp4_tmp = tmp / "output.mp4"

        # Stage 1: WAV → AAC
        logger.info("")
        logger.info("─── Stage 1: WAV → AAC ───")
        transcode_wav_to_aac(wav_path, aac_tmp, bitrate)

        # Stage 2: Mux H264 + AAC → MP4
        logger.info("")
        logger.info("─── Stage 2: Mux H264 + AAC → MP4 (faststart) ───")
        mux_to_mp4(h264_path, aac_tmp, mp4_tmp)

        # Stage 3: Probe output
        logger.info("")
        logger.info("─── Stage 3: ffprobe media metadata ───")
        media_info = probe_mp4(mp4_tmp)

        # Copy final MP4 from temp to output location
        output_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(mp4_tmp, output_path)
        logger.info("✅ Final MP4 copied to: %s", output_path)

    # tmpdir and all intermediates are now cleaned up

    total_elapsed = time.monotonic() - total_t0

    # ── Summary ────────────────────────────────────────────────────────────────
    logger.info("")
    logger.info("=" * 60)
    logger.info("✅  PIPELINE COMPLETE")
    logger.info("=" * 60)
    logger.info("")
    logger.info("  Output:       %s", output_path)
    logger.info("  File size:    %.2f MB", media_info["file_size_bytes"] / (1024 * 1024))
    logger.info("  Duration:     %.1f seconds", media_info["duration_seconds"])
    logger.info("  Resolution:   %s", media_info["resolution"])
    logger.info("  Video codec:  %s", media_info["video_codec"])
    logger.info("  Audio codec:  %s", media_info["audio_codec"])
    logger.info("  Bitrate:      %d kbps", media_info["bitrate_kbps"])
    logger.info("  Total time:   %.1f seconds", total_elapsed)
    logger.info("")

    # Also dump the metadata as JSON for programmatic use
    print("\n── Media metadata (JSON) ──")
    print(json.dumps(media_info, indent=2))

    # Verify the output is playable
    logger.info("")
    logger.info("▶  To play the output:")
    logger.info("   open %s", output_path)
    logger.info("   # or: ffplay %s", output_path)


if __name__ == "__main__":
    main()
