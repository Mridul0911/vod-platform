"""
logger.py — Structured JSON logging for Cloud Logging compatibility.

Every pipeline stage emits a structured log entry with:
  - content_id
  - stage
  - status (info / warning / error)
  - duration_ms (where applicable)
  - arbitrary kwargs for stage-specific fields

Cloud Logging automatically parses JSON from stdout/stderr in Cloud Run.
"""

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any


class StructuredJsonFormatter(logging.Formatter):
    """
    Formats log records as single-line JSON objects compatible with
    Google Cloud Logging's structured logging format.

    Severity mapping:
      DEBUG    → DEBUG
      INFO     → INFO
      WARNING  → WARNING
      ERROR    → ERROR
      CRITICAL → CRITICAL
    """

    SEVERITY_MAP = {
        logging.DEBUG: "DEBUG",
        logging.INFO: "INFO",
        logging.WARNING: "WARNING",
        logging.ERROR: "ERROR",
        logging.CRITICAL: "CRITICAL",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "severity": self.SEVERITY_MAP.get(record.levelno, "DEFAULT"),
            "message": record.getMessage(),
            "logger": record.name,
            "time": datetime.now(timezone.utc).isoformat(),
        }

        # Merge any extra fields passed via extra={} or directly set on the record
        for key, value in record.__dict__.items():
            if key not in (
                "args", "created", "exc_info", "exc_text", "filename",
                "funcName", "levelname", "levelno", "lineno", "message",
                "module", "msecs", "msg", "name", "pathname", "process",
                "processName", "relativeCreated", "stack_info", "thread",
                "threadName",
            ):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def setup_structured_logger(level: int = logging.INFO):
    """Configure the root logger to emit structured JSON to stdout."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(StructuredJsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)


def log_stage(
    logger: logging.Logger,
    content_id: str,
    stage: str,
    level: str = "info",
    **kwargs,
):
    """
    Emit a structured log entry for a named pipeline stage.

    Usage:
        log_stage(logger, content_id, "download_complete", "info",
                  duration_ms=1234, file_size_bytes=56789)
    """
    extra = {"content_id": content_id, "stage": stage, **kwargs}
    log_fn = getattr(logger, level, logger.info)
    log_fn("[%s] stage=%s", content_id, stage, extra=extra)
