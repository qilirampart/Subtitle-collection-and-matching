from __future__ import annotations

import shutil
import sys
from pathlib import Path


def _runtime_executable_name(name: str) -> str:
    return f"{name}.exe" if sys.platform == "win32" else name


def _resolve_resource_root() -> Path:
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            return Path(meipass)
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def _resolve_app_root(resource_root: Path) -> Path:
    if getattr(sys, "frozen", False):
        if sys.platform == "darwin":
            # App bundles under /Applications are normally read-only. Keep user
            # configuration, logs, and downloaded media outside the .app bundle.
            return Path.home() / "Library" / "Application Support" / "Dianzhong" / "YouTube字幕核验助手"
        return Path(sys.executable).resolve().parent
    return resource_root


def _resolve_ffmpeg_dir(resource_root: Path, app_root: Path) -> Path:
    """Prefer user-updatable tools next to the app, then packaged resources."""
    portable_dir = app_root / "runtime" / "ffmpeg"
    if (portable_dir / _runtime_executable_name("ffmpeg")).exists():
        return portable_dir
    bundled_dir = resource_root / "runtime" / "ffmpeg"
    if (bundled_dir / _runtime_executable_name("ffmpeg")).exists():
        return bundled_dir
    return portable_dir


def _resolve_probe_audio_path(resource_root: Path, app_root: Path) -> Path:
    standard_candidates = (
        app_root / "asr_probe.mp3",
        resource_root / "asr_probe.mp3",
    )
    for candidate in standard_candidates:
        if candidate.exists():
            return candidate
    file_name = "闻渊参考音频.MP3"
    candidates = (
        app_root / file_name,
        resource_root / file_name,
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return candidates[1] if getattr(sys, "frozen", False) else candidates[0]


RESOURCE_ROOT = _resolve_resource_root()
APP_ROOT = _resolve_app_root(RESOURCE_ROOT)
PROJECT_ROOT = APP_ROOT
IS_PACKAGED_BUILD = bool(getattr(sys, "frozen", False))

APP_NAME = "素材分析助手"
APP_ORGANIZATION = "Dianzhong"

RUNTIME_DIR = APP_ROOT / "runtime"
FFMPEG_DIR = _resolve_ffmpeg_dir(RESOURCE_ROOT, APP_ROOT)
FFMPEG_EXECUTABLE_PATH = FFMPEG_DIR / _runtime_executable_name("ffmpeg")
FFPROBE_EXECUTABLE_PATH = FFMPEG_DIR / _runtime_executable_name("ffprobe")
YOUTUBE_COMPAT_TOOLS_DIR = RESOURCE_ROOT / "tools"
YOUTUBE_COMPAT_YTDLP_PATH = YOUTUBE_COMPAT_TOOLS_DIR / _runtime_executable_name("yt-dlp-nightly")
YOUTUBE_COMPAT_NODE_PATH = YOUTUBE_COMPAT_TOOLS_DIR / _runtime_executable_name("node")
OUTPUT_DIR = APP_ROOT / "output"
DOWNLOAD_DIR = OUTPUT_DIR / "downloads"
CLIP_OUTPUT_DIR = OUTPUT_DIR / "clips"
EXTRACTED_AUDIO_DIR = OUTPUT_DIR / "audio"
TRANSCRIPT_DIR = OUTPUT_DIR / "transcripts"
SCREENSHOT_DIR = OUTPUT_DIR / "screenshots"
MATERIAL_PACKAGE_DIR = OUTPUT_DIR / "material_packages"
VIDEO_REVIEW_DIR = OUTPUT_DIR / "reviews"
LOG_DIR = OUTPUT_DIR / "logs"
ASR_PROBE_AUDIO_PATH = _resolve_probe_audio_path(RESOURCE_ROOT, APP_ROOT)

DOWNLOADER_CONFIG_PATH = RUNTIME_DIR / "downloader_config.json"
TENCENT_ASR_CONFIG_PATH = RUNTIME_DIR / "tencent_asr_config.json"
API_CONFIG_PATH = RUNTIME_DIR / "api_config.json"
YOUTUBE_COOKIES_PATH = RUNTIME_DIR / "youtube_cookies.txt"
YOUTUBE_PROXY_CONFIG_PATH = RUNTIME_DIR / "youtube_proxy.txt"
SHORTFLIX_COOKIES_PATH = RUNTIME_DIR / "shortflix_cookies.txt"
DOWNLOADER_CONFIG_EXAMPLE_PATH = RESOURCE_ROOT / "runtime" / "downloader_config.example.json"
TENCENT_ASR_CONFIG_EXAMPLE_PATH = RESOURCE_ROOT / "runtime" / "tencent_asr_config.example.json"
API_CONFIG_EXAMPLE_PATH = RESOURCE_ROOT / "runtime" / "api_config.example.json"

ASR_DIRECT_UPLOAD_LIMIT_BYTES = 5 * 1024 * 1024
ASR_AUDIO_CHUNK_SECONDS = 10 * 60

SUPPORTED_VIDEO_EXTENSIONS = {
    ".mp4",
    ".mov",
    ".avi",
    ".mkv",
    ".flv",
    ".wmv",
    ".m4v",
}


def ensure_app_directories() -> None:
    for directory in (
        RUNTIME_DIR,
        FFMPEG_DIR,
        OUTPUT_DIR,
        DOWNLOAD_DIR,
        CLIP_OUTPUT_DIR,
        EXTRACTED_AUDIO_DIR,
        TRANSCRIPT_DIR,
        SCREENSHOT_DIR,
        MATERIAL_PACKAGE_DIR,
        VIDEO_REVIEW_DIR,
        LOG_DIR,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    _ensure_runtime_scaffold()


def _ensure_runtime_scaffold() -> None:
    bundled_items = (
        (DOWNLOADER_CONFIG_EXAMPLE_PATH, RUNTIME_DIR / "downloader_config.example.json"),
        (TENCENT_ASR_CONFIG_EXAMPLE_PATH, RUNTIME_DIR / "tencent_asr_config.example.json"),
        (API_CONFIG_EXAMPLE_PATH, RUNTIME_DIR / "api_config.example.json"),
    )
    for source_path, target_path in bundled_items:
        if source_path.exists() and not target_path.exists():
            shutil.copy2(source_path, target_path)
