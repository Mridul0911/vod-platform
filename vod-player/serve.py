#!/usr/bin/env python3
"""
Local dev server with Range request support for testing the VOD player.

Serves:
  - Player files (vod-player.js, example.html, etc.) from this directory
  - Any MP4 file passed via --media, accessible at /media/<filename>

Range requests are required for seeking in progressive MP4 files.
Python's built-in SimpleHTTPRequestHandler does NOT support Range requests,
so this implements them properly.

Usage:
  python3 serve.py --media '/path/to/video.mp4'
  python3 serve.py --media '/path/to/video.mp4' --port 8080
"""

import argparse
import mimetypes
import os
import sys
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import unquote

# Ensure .js files are served with correct MIME type for ES modules
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("video/mp4", ".mp4")


class RangeRequestHandler(SimpleHTTPRequestHandler):
    """
    HTTP handler that extends SimpleHTTPRequestHandler with:
    1. Range request support (HTTP 206 Partial Content) — needed for MP4 seeking
    2. CORS headers — needed for Shaka Player
    3. /media/<filename> route mapped to the --media file
    """

    media_file_path = None  # set by main()
    media_file_name = None

    def end_headers(self):
        # Add CORS and range headers to every response
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Range")
        self.send_header("Access-Control-Expose-Headers",
                         "Content-Range, Accept-Ranges, Content-Length, Content-Type")
        super().end_headers()

    def do_OPTIONS(self):
        """Handle CORS preflight."""
        self.send_response(204)
        self.end_headers()

    def translate_path(self, path):
        """Map /media/<filename> to the actual media file on disk."""
        path = unquote(path)
        if path.startswith("/media/") and self.media_file_path:
            return str(self.media_file_path)
        return super().translate_path(path)

    def do_GET(self):
        """Handle GET with Range request support."""
        path = self.translate_path(self.path)

        if not os.path.isfile(path):
            super().do_GET()
            return

        file_size = os.path.getsize(path)
        content_type, _ = mimetypes.guess_type(path)
        content_type = content_type or "application/octet-stream"

        range_header = self.headers.get("Range")

        try:
            if range_header:
                # Parse Range: bytes=START-END
                try:
                    range_spec = range_header.replace("bytes=", "").strip()
                    parts = range_spec.split("-")
                    start = int(parts[0]) if parts[0] else 0
                    end = int(parts[1]) if parts[1] else file_size - 1
                except (ValueError, IndexError):
                    self.send_error(416, "Invalid Range")
                    return

                if start >= file_size or end >= file_size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{file_size}")
                    self.end_headers()
                    return

                content_length = end - start + 1

                self.send_response(206)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(content_length))
                self.send_header("Content-Range", f"bytes {start}-{end}/{file_size}")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()

                with open(path, "rb") as f:
                    f.seek(start)
                    remaining = content_length
                    buf_size = 64 * 1024  # 64KB chunks
                    while remaining > 0:
                        chunk = f.read(min(buf_size, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            else:
                # Full response with Accept-Ranges header
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(file_size))
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()

                with open(path, "rb") as f:
                    buf_size = 64 * 1024
                    while True:
                        chunk = f.read(buf_size)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
        except BrokenPipeError:
            pass  # Browser aborted connection (normal during seeking)

    def do_HEAD(self):
        """HEAD requests also need Range awareness."""
        path = self.translate_path(self.path)
        if os.path.isfile(path):
            file_size = os.path.getsize(path)
            content_type, _ = mimetypes.guess_type(path)
            content_type = content_type or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(file_size))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
        else:
            super().do_HEAD()


    def log_message(self, format, *args):
        """Quieter logging — only log media requests and errors."""
        msg = format % args
        if "/media/" in msg or "404" in msg or "500" in msg:
            sys.stderr.write(f"  {msg}\n")


class QuietHTTPServer(HTTPServer):
    """HTTPServer that suppresses BrokenPipeError tracebacks."""
    def handle_error(self, request, client_address):
        err_type = sys.exc_info()[0]
        if err_type in (BrokenPipeError, ConnectionResetError):
            return  # browser aborted — normal during seeking
        super().handle_error(request, client_address)


def main():
    parser = argparse.ArgumentParser(
        description="Local dev server for VOD Player with Range request support"
    )
    parser.add_argument(
        "--media", "-m",
        required=True,
        help="Path to the MP4 file to serve at /media/<filename>",
    )
    parser.add_argument("--port", "-p", type=int, default=8080, help="Port (default: 8080)")
    args = parser.parse_args()

    media_path = Path(args.media).resolve()
    if not media_path.exists():
        print(f"❌ File not found: {media_path}")
        sys.exit(1)

    RangeRequestHandler.media_file_path = media_path
    RangeRequestHandler.media_file_name = media_path.name

    # Serve from the vod-player directory
    os.chdir(Path(__file__).parent)

    server = QuietHTTPServer(("0.0.0.0", args.port), RangeRequestHandler)

    print()
    print("=" * 60)
    print("  VOD Player — Local Dev Server")
    print("=" * 60)
    print()
    print(f"  🎬 Media file:  {media_path.name}")
    print(f"     Full path:   {media_path}")
    print(f"     Size:        {media_path.stat().st_size / (1024*1024):.1f} MB")
    print()
    print(f"  🌐 Player:      http://localhost:{args.port}/example.html")
    print(f"  📹 Media URL:   http://localhost:{args.port}/media/{media_path.name}")
    print()
    print(f"  Range requests: ✅ supported (seeking will work)")
    print(f"  CORS:           ✅ enabled (Access-Control-Allow-Origin: *)")
    print()
    print("  Press Ctrl+C to stop")
    print("=" * 60)
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Server stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
