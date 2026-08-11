from __future__ import annotations

from pathlib import Path

from app.config.settings import APP_ROOT

# In a packaged build, outputs must stay next to the executable rather than
# inside PyInstaller's read-only resource directory.
PROJECT_ROOT = APP_ROOT
RUNTIME_DIR = PROJECT_ROOT / "runtime"
CAPTION_DIR = RUNTIME_DIR / "captions"
AUDIO_DIR = RUNTIME_DIR / "audio"
COVER_DIR = PROJECT_ROOT / "output" / "covers"
TASK_STATE_PATH = RUNTIME_DIR / "session_state.json"
YOUTUBE_COOKIES_PATH = RUNTIME_DIR / "youtube_cookies.txt"
YOUTUBE_PROXY_CONFIG_PATH = RUNTIME_DIR / "youtube_proxy.txt"
ASR_CONFIG_PATH = RUNTIME_DIR / "asr_config.json"


def ensure_runtime_directories() -> None:
    CAPTION_DIR.mkdir(parents=True, exist_ok=True)
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    COVER_DIR.mkdir(parents=True, exist_ok=True)
