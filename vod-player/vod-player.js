/**
 * vod-player.js — Universal Shaka Player Wrapper
 *
 * A framework-agnostic module that wraps shaka-player for VOD playback.
 * Works with React, Vue, Svelte, plain HTML — any environment that can
 * provide a <video> element and import an ES module.
 *
 * TODAY: Progressive MP4 via native browser video engine.
 * FUTURE: DASH (.mpd) or HLS (.m3u8) — no API changes needed. The same
 *         player.load(src) call works for all source types because Shaka
 *         detects the manifest type automatically and falls back to the
 *         browser's native video engine for plain URLs.
 *
 * Usage:
 *   import { VodPlayer } from './vod-player.js';
 *
 *   const player = new VodPlayer(videoElement, {
 *     onError:     (err) => console.error(err),
 *     onBuffering: (isBuffering) => showSpinner(isBuffering),
 *     onLoaded:    (metadata) => console.log('Ready', metadata),
 *     enableUi:    true,   // default: true — Shaka UI overlay
 *   });
 *
 *   await player.load('https://storage.googleapis.com/bucket/video.mp4');
 *   player.play();
 *
 * Signed URL support:
 *   If your source is a signed URL that may expire, catch the error in
 *   onError and check err.isExpiredUrl — if true, fetch a fresh signed
 *   URL and call player.load(newUrl) to reload.
 */

const SHAKA_CDN =
  'https://ajax.googleapis.com/ajax/libs/shaka-player/4.7.11/shaka-player.ui.js';
const SHAKA_CSS =
  'https://ajax.googleapis.com/ajax/libs/shaka-player/4.7.11/controls.css';

/** @type {Promise<void>} cached Shaka load promise */
let _shakaLoadPromise = null;

/**
 * Dynamically load the Shaka Player script and CSS from CDN.
 * Safe to call multiple times — only loads once.
 */
function loadShakaFromCdn() {
  if (_shakaLoadPromise) return _shakaLoadPromise;

  _shakaLoadPromise = new Promise((resolve, reject) => {
    // CSS
    if (!document.querySelector(`link[href="${SHAKA_CSS}"]`)) {
      const link = document.createElement('link');
      link.rel = 'stylesheet';
      link.href = SHAKA_CSS;
      document.head.appendChild(link);
    }

    // JS
    const script = document.createElement('script');
    script.src = SHAKA_CDN;
    script.async = true;
    script.onload = () => resolve();
    script.onerror = () =>
      reject(new Error('Failed to load Shaka Player from CDN'));
    document.head.appendChild(script);
  });

  return _shakaLoadPromise;
}

/**
 * Detect whether an error is a likely expired/forbidden signed URL (403).
 * Shaka error codes: https://shaka-player-demo.appspot.com/docs/api/shaka.util.Error.html
 */
function isExpiredUrlError(shakaError) {
  if (!shakaError || typeof shakaError.code === 'undefined') return false;
  // Shaka BAD_HTTP_STATUS with HTTP 403
  const BAD_HTTP_STATUS = 1001; // shaka.util.Error.Code.BAD_HTTP_STATUS
  return (
    shakaError.code === BAD_HTTP_STATUS &&
    shakaError.data &&
    shakaError.data[1] === 403
  );
}

// ── Error class ─────────────────────────────────────────────────────────────────

export class VodPlayerError extends Error {
  /**
   * @param {string} message
   * @param {object} [shakaError]   original shaka.util.Error object
   * @param {boolean} [isExpiredUrl]
   */
  constructor(message, shakaError, isExpiredUrl = false) {
    super(message);
    this.name = 'VodPlayerError';
    this.shakaError = shakaError || null;
    this.isExpiredUrl = isExpiredUrl;
  }
}

// ── VodPlayer ─────────────────────────────────────────────────────────────────

export class VodPlayer {
  /**
   * @param {HTMLVideoElement} videoElement
   * @param {object} [config]
   * @param {boolean} [config.enableUi=true]            Attach Shaka UI overlay
   * @param {Function} [config.onError]                 (VodPlayerError) => void
   * @param {Function} [config.onBuffering]             (isBuffering: boolean) => void
   * @param {Function} [config.onLoaded]                (metadata: object) => void
   * @param {object} [config.shakaConfig]               Passed directly to player.configure()
   */
  constructor(videoElement, config = {}) {
    if (!(videoElement instanceof HTMLVideoElement)) {
      throw new TypeError('VodPlayer: first argument must be an HTMLVideoElement');
    }

    this._video = videoElement;
    this._config = {
      enableUi: true,
      onError: null,
      onBuffering: null,
      onLoaded: null,
      shakaConfig: {},
      ...config,
    };

    /** @type {shaka.Player|null} */
    this._player = null;
    /** @type {shaka.ui.Overlay|null} */
    this._ui = null;
    /** @type {boolean} */
    this._destroyed = false;

    // Internal ready promise — resolved once Shaka + player are initialized
    this._readyPromise = this._init();
  }

  // ── Public API ─────────────────────────────────────────────────────────────

  /**
   * Load a video source. Accepts:
   *   - Progressive MP4 URL (today)
   *   - DASH manifest (.mpd)  ← no API change needed
   *   - HLS manifest  (.m3u8) ← no API change needed
   *
   * @param {string} src
   * @returns {Promise<void>}
   */
  async load(src) {
    await this._readyPromise;

    if (this._destroyed) {
      throw new VodPlayerError('VodPlayer has been destroyed — create a new instance');
    }

    try {
      await this._player.load(src);
      const metadata = this._extractMetadata();
      this._emit('onLoaded', metadata);
    } catch (err) {
      const expired = isExpiredUrlError(err);
      const wrappedErr = new VodPlayerError(
        expired
          ? 'Source URL is expired or forbidden (403). Refresh the signed URL and call load() again.'
          : `Shaka load error: ${err.message || err}`,
        err,
        expired,
      );
      this._emit('onError', wrappedErr);
      throw wrappedErr;
    }
  }

  /** @returns {Promise<void>} */
  async play() {
    await this._readyPromise;
    return this._video.play();
  }

  pause() {
    this._video.pause();
  }

  /**
   * Seek to a position in seconds.
   * For -movflags +faststart MP4 files the browser issues a Range request
   * and the moov atom at the start of the file allows the browser to calculate
   * the seek target without fetching the entire file first.
   * No custom seek logic needed — native Range-based seeking just works.
   *
   * @param {number} seconds
   */
  seek(seconds) {
    this._video.currentTime = seconds;
  }

  /** @returns {number} current playback position in seconds */
  get currentTime() {
    return this._video.currentTime;
  }

  /** @returns {number} total duration in seconds (or NaN if not loaded) */
  get duration() {
    return this._video.duration;
  }

  /** @returns {boolean} */
  get paused() {
    return this._video.paused;
  }

  /**
   * The underlying shaka.Player instance for advanced configuration or
   * direct API access. Consumers using adaptive streaming can call
   * player.shakaPlayer.getAbrManager() etc.
   *
   * @returns {shaka.Player|null}
   */
  get shakaPlayer() {
    return this._player;
  }

  /**
   * Tear down the player and release all resources.
   * @returns {Promise<void>}
   */
  async destroy() {
    if (this._destroyed) return;
    this._destroyed = true;

    if (this._ui) {
      await this._ui.destroy();
      this._ui = null;
    }

    if (this._player) {
      await this._player.destroy();
      this._player = null;
    }
  }

  // ── Private ────────────────────────────────────────────────────────────────

  /**
   * Initialize Shaka and install polyfills.
   * Resolves the internal _readyPromise.
   */
  async _init() {
    // Load Shaka from CDN if shaka is not already available globally
    if (typeof shaka === 'undefined') {
      await loadShakaFromCdn();
    }

    // Install browser polyfills (required for MSE-based ABR later)
    shaka.polyfill.installAll();

    // Check browser support
    if (!shaka.Player.isBrowserSupported()) {
      throw new VodPlayerError(
        'This browser does not support the required media APIs for Shaka Player.',
      );
    }

    // Create the player
    this._player = new shaka.Player(this._video);

    // Apply any consumer-supplied configuration
    if (Object.keys(this._config.shakaConfig).length > 0) {
      this._player.configure(this._config.shakaConfig);
    }

    // Wire up Shaka UI overlay
    if (this._config.enableUi) {
      const container = this._video.parentElement;
      if (container) {
        this._ui = new shaka.ui.Overlay(this._player, container, this._video);
        this._ui.configure({
          addSeekBar: true,
          addBigPlayButton: true,
          controlPanelElements: [
            'play_pause',
            'time_and_duration',
            'spacer',
            'mute',
            'volume',
            'fullscreen',
            'overflow_menu',
          ],
          overflowMenuButtons: ['quality', 'language', 'playback_rate'],
        });
      } else {
        console.warn(
          'VodPlayer: enableUi=true but videoElement has no parentElement. ' +
          'The UI overlay requires a container. Wrap the <video> in a <div>.',
        );
      }
    }

    // ── Event wiring ─────────────────────────────────────────────────────────

    // Shaka error events
    this._player.addEventListener('error', (event) => {
      const err = event.detail;
      const expired = isExpiredUrlError(err);
      const wrappedErr = new VodPlayerError(
        expired
          ? 'Source URL expired (403). Call load() with a fresh URL.'
          : `Playback error: ${err.message || JSON.stringify(err)}`,
        err,
        expired,
      );
      this._emit('onError', wrappedErr);
    });

    // Buffering state — fires on seek, initial load, and stalls
    this._player.addEventListener('buffering', (event) => {
      this._emit('onBuffering', event.buffering);
    });

    // Native video events that complement Shaka's buffering events
    this._video.addEventListener('waiting', () => this._emit('onBuffering', true));
    this._video.addEventListener('playing', () => this._emit('onBuffering', false));
    this._video.addEventListener('canplay', () => this._emit('onBuffering', false));
  }

  _emit(hookName, ...args) {
    const fn = this._config[hookName];
    if (typeof fn === 'function') {
      try {
        fn(...args);
      } catch (e) {
        console.error(`VodPlayer: uncaught error in ${hookName} callback`, e);
      }
    }
  }

  _extractMetadata() {
    return {
      duration: this._video.duration,
      videoWidth: this._video.videoWidth,
      videoHeight: this._video.videoHeight,
    };
  }
}

// ── Default export for convenience ────────────────────────────────────────────
export default VodPlayer;
