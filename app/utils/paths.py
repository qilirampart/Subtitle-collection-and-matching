from __future__ import annotations

from datetime import datetime
from pathlib import Path

from app.config.settings import CLIP_OUTPUT_DIR, DOWNLOAD_DIR, LOG_DIR, OUTPUT_DIR, PROJECT_ROOT, SCREENSHOT_DIR

_COUNTERS: dict[str, tuple[str, int]] = {}


def ensure_output_directories() -> None:
    for directory in (OUTPUT_DIR, SCREENSHOT_DIR, DOWNLOAD_DIR, CLIP_OUTPUT_DIR, LOG_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def compact_timestamp() -> str:
    return datetime.now().strftime("%y%m%d_%H%M%S")


def next_compact_name(prefix: str) -> str:
    stamp = compact_timestamp()
    previous_stamp, count = _COUNTERS.get(prefix, ("", 0))
    count = count + 1 if previous_stamp == stamp else 1
    _COUNTERS[prefix] = (stamp, count)
    return f"{prefix}_{stamp}_{count:02d}"


def _normalized_suffix(suffix: str) -> str:
    suffix = str(suffix or "").strip()
    if not suffix:
        return ".mp4"
    return suffix if suffix.startswith(".") else f".{suffix}"


def build_screenshot_session_dir(video_name: str) -> Path:
    session_dir = SCREENSHOT_DIR / next_compact_name("frames")
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def build_download_output_path(seed_name: str, suffix: str = ".mp4") -> Path:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    return DOWNLOAD_DIR / f"{next_compact_name('video')}{_normalized_suffix(suffix)}"


def build_article_session_dir(seed_name: str) -> Path:
    session_dir = DOWNLOAD_DIR / next_compact_name("article")
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def build_clip_output_path(
    seed_name: str,
    *,
    start_ms: int = 0,
    duration_ms: int | None = None,
    suffix: str = ".mp4",
) -> Path:
    start_seconds = max(0, int(start_ms // 1000))
    if duration_ms is None:
        range_suffix = f"s{start_seconds:03d}"
    else:
        duration_seconds = max(1, int((duration_ms + 999) // 1000))
        range_suffix = f"s{start_seconds:03d}_d{duration_seconds:03d}"
    CLIP_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return CLIP_OUTPUT_DIR / f"{next_compact_name('clip')}_{range_suffix}{_normalized_suffix(suffix)}"


__all__ = [
    "PROJECT_ROOT",
    "OUTPUT_DIR",
    "SCREENSHOT_DIR",
    "DOWNLOAD_DIR",
    "CLIP_OUTPUT_DIR",
    "LOG_DIR",
    "ensure_output_directories",
    "compact_timestamp",
    "next_compact_name",
    "build_screenshot_session_dir",
    "build_download_output_path",
    "build_article_session_dir",
    "build_clip_output_path",
]
