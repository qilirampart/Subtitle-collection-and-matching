from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from app.settings import RUNTIME_DIR, YOUTUBE_COOKIES_PATH


_BROWSER_COOKIES_PATH = RUNTIME_DIR / "youtube_browser_profile" / "Cookies"
_ALLOWED_DOMAINS = ("youtube.com", "google.com")
_AUTH_COOKIE_NAMES = {
    "APISID",
    "HSID",
    "LOGIN_INFO",
    "SAPISID",
    "SID",
    "SSID",
    "__SECURE-1PAPISID",
    "__SECURE-1PSID",
    "__SECURE-3PAPISID",
    "__SECURE-3PSID",
}
_CHROME_EPOCH_OFFSET_SECONDS = 11_644_473_600


class YouTubeCookieSyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class YouTubeCookieSyncResult:
    cookie_count: int
    authenticated: bool


def sync_browser_cookie_file() -> YouTubeCookieSyncResult:
    """Export local Chromium YouTube/Google cookies in Netscape format for yt-dlp."""
    rows = _read_cookie_rows(_BROWSER_COOKIES_PATH)
    if not rows:
        raise YouTubeCookieSyncError("未找到内置浏览器 Cookie。请先打开登录窗口并完成 YouTube 登录。")

    lines = [
        "# Netscape HTTP Cookie File",
        "# Created locally by YouTube 字幕核验助手。请勿外传。",
    ]
    auth_names: set[str] = set()
    for domain, path, secure, http_only, expires_utc, name, value in rows:
        auth_names.add(name.upper())
        expires = _chrome_expiry_to_unix(expires_utc)
        lines.append(
            "\t".join(
                (
                    f"#HttpOnly_{domain}" if http_only else domain,
                    "TRUE" if domain.startswith(".") else "FALSE",
                    path or "/",
                    "TRUE" if secure else "FALSE",
                    str(expires),
                    name,
                    value.replace("\t", "").replace("\r", "").replace("\n", ""),
                )
            )
        )

    YOUTUBE_COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    YOUTUBE_COOKIES_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return YouTubeCookieSyncResult(
        cookie_count=len(rows),
        authenticated=bool(auth_names & _AUTH_COOKIE_NAMES),
    )


def _read_cookie_rows(cookie_path: Path) -> list[tuple[str, str, bool, bool, int, str, str]]:
    if not cookie_path.is_file():
        return []
    last_error: sqlite3.Error | None = None
    for _attempt in range(8):
        try:
            connection = sqlite3.connect(cookie_path.as_uri() + "?mode=ro", uri=True, timeout=0.5)
            try:
                raw_rows = connection.execute(
                    "SELECT host_key, path, is_secure, is_httponly, expires_utc, name, value FROM cookies"
                ).fetchall()
            finally:
                connection.close()
            return [
                (
                    str(domain),
                    str(path or "/"),
                    bool(secure),
                    bool(http_only),
                    int(expires_utc or 0),
                    str(name),
                    str(value),
                )
                for domain, path, secure, http_only, expires_utc, name, value in raw_rows
                if str(domain).lstrip(".").endswith(_ALLOWED_DOMAINS) and str(name)
            ]
        except sqlite3.Error as exc:
            last_error = exc
            time.sleep(0.25)
    raise YouTubeCookieSyncError(f"浏览器 Cookie 数据库仍被占用，请关闭登录窗口后重试：{last_error}")


def _chrome_expiry_to_unix(value: int) -> int:
    if value <= 0:
        return 0
    return max(0, int(value // 1_000_000 - _CHROME_EPOCH_OFFSET_SECONDS))


__all__ = ["YouTubeCookieSyncError", "YouTubeCookieSyncResult", "sync_browser_cookie_file"]
