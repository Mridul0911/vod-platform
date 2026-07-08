# ── Stage 1: Build ─────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /app

# Install build tools then pip dependencies into /app/deps
RUN apt-get update && apt-get install -y --no-install-recommends gcc && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/app/deps -r requirements.txt


# ── Stage 2: Runtime ────────────────────────────────────────────────────────────
FROM python:3.12-slim

# Install ffmpeg (includes ffprobe) — use the Debian package for a
# reproducible, pre-built binary. Pin to the buster-backports version
# for a stable build.
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Verify ffmpeg is available at expected paths
RUN ffmpeg -version && ffprobe -version

WORKDIR /app

# Copy installed Python packages from builder stage
COPY --from=builder /app/deps /usr/local

# Copy application source
COPY . .

# Run as non-root user
RUN useradd -m -u 1001 appuser
USER appuser

# Cloud Run sets PORT automatically; default to 8080
ENV PORT=8080

# gunicorn: single worker per instance (CPU-bound ffmpeg work),
# longer timeout to match JOB_TIMEOUT_SECONDS + overhead.
# Cloud Run concurrency=1 is set at deploy time — one request at a time per instance.
CMD ["gunicorn", \
     "--bind", "0.0.0.0:8080", \
     "--workers", "1", \
     "--threads", "1", \
     "--timeout", "660", \
     "--access-logfile", "-", \
     "--error-logfile", "-", \
     "--log-level", "info", \
     "main:app"]
