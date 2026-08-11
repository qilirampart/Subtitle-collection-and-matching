from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.config.settings import LOG_DIR

_LOGGING_READY = False


def configure_logging() -> None:
    global _LOGGING_READY
    if _LOGGING_READY:
        return

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = Path(LOG_DIR) / "app.log"
    active_log_file: Path | None = log_file
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s - %(message)s")

    try:
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=2 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
    except OSError:
        # A stale viewer or another process must not prevent the application from starting.
        fallback_file = Path(LOG_DIR) / f"app_{os.getpid()}.log"
        try:
            file_handler = logging.FileHandler(fallback_file, encoding="utf-8")
            active_log_file = fallback_file
        except OSError:
            file_handler = None
            active_log_file = None
    if file_handler is not None:
        file_handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()
    if file_handler is not None:
        root_logger.addHandler(file_handler)

    stream = getattr(sys, "stderr", None) or getattr(sys, "stdout", None)
    if getattr(stream, "write", None) is not None:
        stream_handler = logging.StreamHandler(stream)
        stream_handler.setFormatter(formatter)
        root_logger.addHandler(stream_handler)

    _LOGGING_READY = True
    root_logger.info("Logging initialized. log_file=%s", active_log_file or "console only")


def get_logger(name: str) -> logging.Logger:
    configure_logging()
    return logging.getLogger(name)
