# VOD Platform

End-to-end VOD pipeline: GCS-triggered transcode-mux service + universal Shaka Player wrapper.

```
vod-platform/
├── transcode-service/          # Part 1 — Cloud Run transcode-mux service
│   ├── main.py                 # Flask app, Pub/Sub push handler, auth gate
│   ├── pipeline.py             # Download → WAV→AAC → mux → probe → upload
│   ├── gcs_client.py           # Streaming GCS I/O (no full in-memory buffering)
│   ├── metadata_store.py       # Firestore store + transactional dedup locking
│   ├── auth.py                 # Google OIDC token verification
│   ├── logger.py               # Structured JSON logger for Cloud Logging
│   ├── requirements.txt
│   ├── Dockerfile
│   ├── deploy.sh
│   └── README.md
└── vod-player/                 # Part 2 — Universal Shaka Player wrapper
    ├── vod-player.js           # ES module — framework-agnostic Shaka wrapper
    ├── VodPlayer.jsx           # React component wrapping the module
    ├── example.html            # Plain HTML standalone usage
    ├── cors.json               # GCS bucket CORS config (gsutil cors set)
    └── README.md
```

---

## Adaptive Bitrate Migration Path

When you're ready to add multiple renditions and adaptive streaming, here's
what changes in each part:

### Part 1 — Transcode Service

**What changes:**
1. The single-MP4 output becomes **multiple H264 renditions** (e.g. 360p, 720p, 1080p), each
   produced by a separate ffmpeg re-encode pass (no longer pure stream-copy for all renditions).
2. Add **Shaka Packager** as a second stage after ffmpeg: it reads the rendition MP4s and
   produces a DASH MPD + CMAF segments (or HLS), which are uploaded to GCS alongside a manifest.
3. The `output_gcs_path` in Firestore changes from a single `.mp4` to a manifest path:
   `processed/{content_id}/manifest.mpd`.
4. Because you now have re-encode passes (CPU-heavy), increase Cloud Run `--cpu` and `--memory`
   and adjust `FFMPEG_THREADS` accordingly. Consider also bumping `--concurrency` back to 1
   and using a job queue (Cloud Tasks) to fan out renditions across multiple instances in parallel.

**What stays the same:**
- Download / upload / Firestore / auth / dedup logic — unchanged.
- The Pub/Sub payload / trigger contract — unchanged.
- `ffprobe` stage — still runs on the final output.

### Part 2 — Player Wrapper

**What changes:**
- The `src` URL passed to `player.load()` changes from `.mp4` → `.mpd` (or `.m3u8`).
- Shaka switches from native-video-engine mode to its MSE-based ABR engine automatically.
- You may want to pass ABR configuration via `shakaConfig` in the constructor.

**What stays the same:**
- `player.load(src)`, `play()`, `pause()`, `seek()`, `destroy()` — **zero API changes**.
- All event hooks (`onError`, `onBuffering`, `onLoaded`) — unchanged.
- React component (`VodPlayer.jsx`) — zero changes; just pass the new `.mpd` URL as `src`.

This is the key design goal of the wrapper: the consumer's code doesn't change when the
pipeline evolves from progressive MP4 to adaptive streaming.
