# VOD Player — Universal Shaka Player Wrapper

A framework-agnostic ES module that wraps Shaka Player for VOD playback.
Drop it into React, Vue, Svelte, or plain HTML in under 10 lines.

---

## Quick Start (any framework)

```js
import { VodPlayer } from './vod-player.js';

const player = new VodPlayer(document.getElementById('video'), {
  onLoaded:    (meta) => console.log('Ready', meta),
  onBuffering: (isBuf) => showSpinner(isBuf),
  onError:     (err) => console.error(err),
});

await player.load('https://storage.googleapis.com/bucket/processed/id/id.mp4');
player.play();
```

**That's it. 6 lines.**

---

## HTML container requirement

Shaka's UI overlay needs a `<div>` wrapping the `<video>`:

```html
<div id="player-container">
  <video id="video"></video>
</div>
```

The wrapper reads `videoElement.parentElement` to attach the overlay. If you
don't need the built-in UI set `enableUi: false`.

---

## API Reference

```ts
new VodPlayer(videoElement: HTMLVideoElement, config?: {
  enableUi?:    boolean;          // default: true — Shaka UI overlay
  onError?:     (err: VodPlayerError) => void;
  onBuffering?: (isBuffering: boolean) => void;
  onLoaded?:    (metadata: { duration, videoWidth, videoHeight }) => void;
  shakaConfig?: object;           // passed to player.configure()
})
```

| Method                | Description                                               |
|-----------------------|-----------------------------------------------------------|
| `load(src)`           | Load any URL: progressive MP4, DASH `.mpd`, HLS `.m3u8`  |
| `play()`              | Start/resume playback                                     |
| `pause()`             | Pause playback                                            |
| `seek(seconds)`       | Jump to position (Range-request based — no custom logic)  |
| `destroy()`           | Tear down player and release resources                    |
| `.shakaPlayer`        | Raw `shaka.Player` instance for advanced use              |
| `.currentTime`        | Current playback position (seconds)                       |
| `.duration`           | Total duration (seconds)                                  |
| `.paused`             | Boolean                                                   |

---

## React

```jsx
import VodPlayerComponent from './VodPlayer';

<VodPlayerComponent
  src="https://storage.googleapis.com/.../video.mp4"
  enableUi={true}
  onError={(err) => { if (err.isExpiredUrl) refreshUrl(); }}
/>
```

When `src` prop changes, the player automatically calls `load(newSrc)`.

---

## Vue / Svelte / plain HTML

See [`example.html`](./example.html) for a complete plain HTML integration.
For Vue/Svelte, follow the same pattern as React: create the player in
`onMounted` / `onMount`, call `player.load(src)` when the source changes,
and call `player.destroy()` in `onUnmounted` / `onDestroy`.

---

## GCS CORS Setup

Range-request seeking requires the `Accept-Ranges` and `Content-Range`
headers to pass through unstripped. Apply the included `cors.json`:

```bash
# Edit cors.json to add your actual frontend origin(s)
gsutil cors set cors.json gs://YOUR_PROCESSED_BUCKET
gsutil cors get gs://YOUR_PROCESSED_BUCKET  # verify
```

**Why this matters for seeking:**
Progressive MP4 with `-movflags +faststart` places the `moov` atom at the
beginning of the file. When the user seeks, the browser issues a Range request
(e.g. `Range: bytes=4194304-`) to the GCS object. If CORS headers are missing
or `Content-Range` is stripped, the browser treats the response as a cross-origin
failure and seeking stalls or breaks. The `cors.json` in this repo explicitly
allows `Content-Range` and `Accept-Ranges` through.

**CDN / proxy note:** If you proxy GCS through Cloud CDN, Cloudflare, or any
other CDN, ensure the CDN is configured to:
1. Forward `Range` request headers to GCS (pass-through mode, not aggregating).
2. Not strip `Accept-Ranges`, `Content-Range`, or `ETag` from responses.
3. Cache partial content (206 responses) correctly if you enable edge caching.

---

## Signed URL Support

If your MP4 is served via a signed URL that expires:

```js
const player = new VodPlayer(videoEl, {
  onError(err) {
    if (err.isExpiredUrl) {
      // URL returned HTTP 403 — fetch a fresh signed URL from your backend
      fetch('/api/signed-url?content_id=abc123')
        .then(r => r.json())
        .then(({ url }) => player.load(url));
    }
  },
});
```

`err.isExpiredUrl` is `true` when Shaka reports `BAD_HTTP_STATUS` with HTTP 403,
which is the error GCS signed URLs return when expired. You can also pre-emptively
refresh by tracking the signed URL's expiry time and calling `player.load(newUrl)`
before it expires.

---

## Switching to Adaptive Streaming (DASH/HLS) Later

**No wrapper API changes needed.** The same `player.load(src)` call works:

```js
// Today (progressive MP4):
await player.load('https://.../video.mp4');

// Tomorrow (DASH — Shaka natively handles it):
await player.load('https://.../manifest.mpd');

// Or HLS:
await player.load('https://.../playlist.m3u8');
```

Shaka detects the source type from the URL/content-type and switches between
its native MSE-based ABR engine (for DASH/HLS) and the browser's native video
engine (for plain MP4). See the migration notes section in the platform README.
