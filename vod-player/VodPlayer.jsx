/**
 * VodPlayer.jsx — Minimal React component wrapping the VodPlayer module.
 *
 * Usage:
 *   import VodPlayerComponent from './VodPlayer';
 *
 *   <VodPlayerComponent
 *     src="https://storage.googleapis.com/bucket/.../video.mp4"
 *     enableUi={true}
 *     onError={(err) => console.error(err)}
 *   />
 *
 * Works with React 18+. No build-tool changes needed — vod-player.js is
 * a plain ES module. If bundling, ensure your bundler resolves it.
 *
 * Signed URL refresh:
 *   Pass a fresh URL as the `src` prop when it expires — the useEffect
 *   dependency on `src` will automatically call player.load(newSrc).
 */

import React, { useRef, useEffect, useState } from 'react';
import { VodPlayer } from './vod-player.js';

/**
 * @param {object} props
 * @param {string}   props.src        Video URL (MP4, DASH .mpd, or HLS .m3u8)
 * @param {boolean}  [props.enableUi] Show Shaka UI overlay (default: true)
 * @param {Function} [props.onError]  (VodPlayerError) => void
 * @param {Function} [props.onLoaded] (metadata) => void
 * @param {object}   [props.style]    Extra styles for the outer container
 */
export default function VodPlayerComponent({
  src,
  enableUi = true,
  onError,
  onLoaded,
  style,
}) {
  const containerRef = useRef(null);  // div wrapping <video> — required for Shaka UI
  const videoRef     = useRef(null);
  const playerRef    = useRef(null);  // VodPlayer instance

  const [status, setStatus] = useState('idle');   // 'idle' | 'loading' | 'ready' | 'buffering' | 'error'
  const [errorMsg, setErrorMsg] = useState('');
  const [metadata, setMetadata] = useState(null);

  // ── Init player on mount, destroy on unmount ────────────────────────────────
  useEffect(() => {
    const videoEl = videoRef.current;
    if (!videoEl) return;

    const player = new VodPlayer(videoEl, {
      enableUi,

      onLoaded(meta) {
        setStatus('ready');
        setMetadata(meta);
        onLoaded?.(meta);
      },

      onBuffering(isBuffering) {
        setStatus(isBuffering ? 'buffering' : 'ready');
      },

      onError(err) {
        setStatus('error');
        setErrorMsg(err.message);
        onError?.(err);
      },
    });

    playerRef.current = player;

    return () => {
      // Cleanup on unmount (also handles hot-reload dev scenarios)
      player.destroy();
      playerRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []); // run once on mount only

  // ── Load/reload when src changes ────────────────────────────────────────────
  useEffect(() => {
    if (!src || !playerRef.current) return;
    setStatus('loading');
    setErrorMsg('');
    playerRef.current.load(src).catch(() => {
      // onError callback already invoked by VodPlayer — nothing to do here
    });
  }, [src]);

  // ── Expose shakaPlayer for parent use via imperative handle ────────────────
  // If you need ref access from a parent, wrap this in React.forwardRef +
  // useImperativeHandle to expose play/pause/seek/shakaPlayer.

  return (
    <div
      ref={containerRef}
      style={{
        position: 'relative',
        width: '100%',
        aspectRatio: '16 / 9',
        background: '#000',
        borderRadius: 8,
        overflow: 'hidden',
        ...style,
      }}
    >
      {/* Shaka UI overlay is attached to this container div */}
      <video
        ref={videoRef}
        style={{ width: '100%', height: '100%', display: 'block' }}
        preload="metadata"
      />

      {/* Loading overlay */}
      {status === 'loading' && (
        <div style={overlayStyle}>
          <Spinner />
          <span style={{ marginTop: 12, color: '#ccc', fontSize: 14 }}>Loading…</span>
        </div>
      )}

      {/* Buffering indicator (small pill in corner) */}
      {status === 'buffering' && (
        <div style={bufferingPillStyle}>
          <span style={{ animation: 'pulse 1s infinite' }}>⏳</span> Buffering
        </div>
      )}

      {/* Error overlay */}
      {status === 'error' && (
        <div style={overlayStyle}>
          <span style={{ fontSize: 32 }}>⚠️</span>
          <span style={{ marginTop: 8, color: '#fca5a5', fontSize: 14, textAlign: 'center', padding: '0 16px' }}>
            {errorMsg}
          </span>
        </div>
      )}

      <style>{`
        @keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
        @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity: 0.4; } }
      `}</style>
    </div>
  );
}

// ── Inline styles ─────────────────────────────────────────────────────────────

const overlayStyle = {
  position: 'absolute',
  inset: 0,
  display: 'flex',
  flexDirection: 'column',
  alignItems: 'center',
  justifyContent: 'center',
  background: 'rgba(0,0,0,0.6)',
  color: '#fff',
};

const bufferingPillStyle = {
  position: 'absolute',
  top: 12,
  right: 12,
  background: 'rgba(0,0,0,0.65)',
  color: '#fcd34d',
  padding: '4px 10px',
  borderRadius: 20,
  fontSize: 12,
  display: 'flex',
  alignItems: 'center',
  gap: 6,
};

function Spinner() {
  return (
    <div
      style={{
        width: 40,
        height: 40,
        border: '3px solid rgba(255,255,255,0.2)',
        borderTopColor: '#60a5fa',
        borderRadius: '50%',
        animation: 'spin 0.8s linear infinite',
      }}
    />
  );
}

// ── Usage example (for reference — not exported) ──────────────────────────────
/*
function App() {
  const [src, setSrc] = React.useState(
    'https://storage.googleapis.com/YOUR_BUCKET/processed/movie-abc/movie-abc.mp4'
  );

  function handleError(err) {
    if (err.isExpiredUrl) {
      // Signed URL expired — fetch a new one from your backend
      fetch('/api/signed-url?content_id=movie-abc')
        .then(r => r.json())
        .then(({ url }) => setSrc(url));   // triggers player.load(url) automatically
    }
  }

  return (
    <div style={{ maxWidth: 900, margin: '0 auto' }}>
      <VodPlayerComponent
        src={src}
        enableUi={true}
        onError={handleError}
        onLoaded={meta => console.log('Video ready', meta)}
      />
    </div>
  );
}
*/
