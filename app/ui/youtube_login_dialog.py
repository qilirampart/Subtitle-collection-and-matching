from __future__ import annotations

from PySide6.QtCore import QTimer, QUrl, Signal
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout

from app.settings import RUNTIME_DIR, YOUTUBE_COOKIES_PATH
from app.ui.window_geometry import apply_responsive_window_geometry


class YouTubeLoginDialog(QDialog):
    """Non-modal YouTube login window that exports its own cookie file."""

    status_changed = Signal(str)
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

    def __init__(self, target_url: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("YouTube 登录与下载授权")
        apply_responsive_window_geometry(
            self,
            preferred_width=1100,
            preferred_height=760,
            minimum_width=760,
            minimum_height=480,
        )
        self.setModal(False)

        self._cookie_store = None
        self._known_cookies: dict[tuple[str, str, str], tuple[str, bool, bool, int]] = {}
        self._pending_cookies: dict[tuple[str, str, str], tuple[str, bool, bool, int]] = {}
        self._sync_active = False
        self._sync_timer = QTimer(self)
        self._sync_timer.setSingleShot(True)
        self._sync_timer.setInterval(1200)
        self._sync_timer.timeout.connect(self._finish_cookie_sync)

        self.status_label = QLabel("完成登录后直接关闭本窗口，主程序会自动同步账号登录状态。")
        self.status_label.setWordWrap(True)
        self.open_button = QPushButton("刷新页面")
        self.open_button.clicked.connect(self._reload)
        self.sync_button = QPushButton("保存并关闭")
        self.sync_button.clicked.connect(self.close)
        self.clear_button = QPushButton("清除登录状态")
        self.clear_button.clicked.connect(self.clear_cookies)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.close)

        toolbar = QHBoxLayout()
        toolbar.addWidget(self.status_label, 1)
        toolbar.addWidget(self.open_button)
        toolbar.addWidget(self.sync_button)
        toolbar.addWidget(self.clear_button)
        toolbar.addWidget(close_button)

        self.browser_host = QVBoxLayout()
        root = QVBoxLayout(self)
        root.addLayout(toolbar)
        root.addLayout(self.browser_host, 1)

        self._browser = self._create_browser()
        self.browser_host.addWidget(self._browser)
        self._browser.setUrl(QUrl.fromUserInput(target_url or "https://www.youtube.com/"))

    def _create_browser(self):
        from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
        from PySide6.QtWebEngineWidgets import QWebEngineView

        profile_dir = RUNTIME_DIR / "youtube_browser_profile"
        profile_dir.mkdir(parents=True, exist_ok=True)
        profile = QWebEngineProfile("youtube-login", self)
        profile.setPersistentStoragePath(str(profile_dir))
        profile.setCachePath(str(profile_dir / "cache"))
        profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
        page = QWebEnginePage(profile, self)
        browser = QWebEngineView(self)
        browser.setPage(page)
        self._cookie_store = profile.cookieStore()
        self._cookie_store.cookieAdded.connect(self._handle_cookie_added)
        self._cookie_store.cookieRemoved.connect(self._handle_cookie_removed)
        return browser

    def _reload(self) -> None:
        self._browser.reload()

    @staticmethod
    def _cookie_text(value) -> str:
        try:
            return value.data().decode("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            return bytes(value).decode("utf-8", errors="ignore")

    def sync_cookies(self) -> None:
        if self._cookie_store is None:
            return
        # QWebEngine may have emitted cookieAdded while the page was loading, before
        # the user pressed Sync. Start from that cache, then request a fresh replay.
        self._pending_cookies = dict(self._known_cookies)
        self._sync_active = True
        self.status_label.setText("正在读取 YouTube 登录状态...")
        self._cookie_store.loadAllCookies()
        self._sync_timer.start()

    def _handle_cookie_added(self, cookie) -> None:
        domain = str(cookie.domain() or "").strip().lower()
        if not (domain.endswith("youtube.com") or domain.endswith("google.com")):
            return
        name = self._cookie_text(cookie.name()).strip()
        if not name:
            return
        value = self._cookie_text(cookie.value()).replace("\t", "").replace("\r", "").replace("\n", "")
        path = str(cookie.path() or "/").strip() or "/"
        expiration = cookie.expirationDate()
        expires = int(expiration.toSecsSinceEpoch()) if expiration.isValid() else 0
        cookie_key = (domain, path, name)
        cookie_value = (
            value,
            bool(cookie.isSecure()),
            bool(cookie.isHttpOnly()),
            max(0, expires),
        )
        self._known_cookies[cookie_key] = cookie_value
        if self._sync_active:
            self._pending_cookies[cookie_key] = cookie_value

    def _handle_cookie_removed(self, cookie) -> None:
        domain = str(cookie.domain() or "").strip().lower()
        name = self._cookie_text(cookie.name()).strip()
        path = str(cookie.path() or "/").strip() or "/"
        if domain and name:
            key = (domain, path, name)
            self._known_cookies.pop(key, None)
            self._pending_cookies.pop(key, None)

    def _finish_cookie_sync(self) -> None:
        self._sync_active = False
        if not self._pending_cookies:
            self.status_label.setText("未读取到 Cookie，请确认已完成 YouTube 登录后重试。")
            return
        lines = [
            "# Netscape HTTP Cookie File",
            "# Created by YouTube 字幕核验助手，请勿外传。",
        ]
        for (domain, path, name), (value, secure, http_only, expires) in sorted(self._pending_cookies.items()):
            file_domain = f"#HttpOnly_{domain}" if http_only else domain
            include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
            lines.append("\t".join((
                file_domain,
                include_subdomains,
                path,
                "TRUE" if secure else "FALSE",
                str(expires),
                name,
                value,
            )))
        try:
            YOUTUBE_COOKIES_PATH.parent.mkdir(parents=True, exist_ok=True)
            YOUTUBE_COOKIES_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError as exc:
            QMessageBox.warning(self, "同步失败", f"无法保存登录状态：{exc}")
            return
        authenticated = self._has_authenticated_session(self._pending_cookies)
        if authenticated:
            self.status_label.setText(
                f"已同步 {len(self._pending_cookies)} 个 Cookie，已检测到账号登录状态，后续下载会自动使用。"
            )
        else:
            self.status_label.setText(
                f"已同步 {len(self._pending_cookies)} 个基础 Cookie，但未检测到账号登录状态。"
                "请确认右上角显示账号头像（不是“登录”）后再同步。"
            )

    def clear_cookies(self) -> None:
        try:
            YOUTUBE_COOKIES_PATH.unlink(missing_ok=True)
            if self._cookie_store is not None:
                self._cookie_store.deleteAllCookies()
            self._known_cookies.clear()
            self._pending_cookies.clear()
        except OSError as exc:
            QMessageBox.warning(self, "清除失败", f"无法清除登录状态：{exc}")
            return
        self.status_label.setText("已清除同步的 Cookie。")

    @staticmethod
    def _initial_status() -> str:
        if YOUTUBE_COOKIES_PATH.is_file() and YOUTUBE_COOKIES_PATH.stat().st_size > 0:
            return "已有同步登录状态。需要更新时，请在下方登录后点击“同步登录状态”。"
        return "请在下方登录 YouTube；登录完成后点击“同步登录状态”。"

    @classmethod
    def _has_authenticated_session(
        cls,
        cookies: dict[tuple[str, str, str], tuple[str, bool, bool, int]],
    ) -> bool:
        return any(name.upper() in cls._AUTH_COOKIE_NAMES for _domain, _path, name in cookies)


__all__ = ["YouTubeLoginDialog"]
