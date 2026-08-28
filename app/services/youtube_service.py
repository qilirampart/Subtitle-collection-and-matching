from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlsplit, urlunsplit

from app.config.settings import (
    FFMPEG_DIR,
    YOUTUBE_COMPAT_NODE_PATH,
    YOUTUBE_COMPAT_YTDLP_PATH,
    YOUTUBE_COOKIES_PATH,
    YOUTUBE_PROXY_CONFIG_PATH,
)
from app.utils.ffmpeg import _hidden_process_kwargs
from app.utils.logger import get_logger
from app.utils.paths import build_download_output_path


LOGGER = get_logger(__name__)


class YouTubeServiceError(RuntimeError):
    pass


class YouTubeDownloadCancelled(YouTubeServiceError):
    pass


@dataclass(frozen=True)
class YouTubeCollectedVideo:
    video_id: str
    share_url: str
    title: str
    channel: str | None = None
    channel_id: str | None = None
    duration_seconds: int | None = None
    upload_date: str | None = None
    view_count: int | None = None
    thumbnail_url: str | None = None


@dataclass(frozen=True)
class YouTubeCollectionResult:
    source_url: str
    channel: str | None
    channel_id: str | None
    videos: list[YouTubeCollectedVideo]


@dataclass(frozen=True)
class YouTubeDownloadResult:
    share_url: str
    resolved_url: str
    local_path: str
    title: str | None = None
    author: str | None = None
    media_url: str | None = None


class YouTubeService:
    """Collect public YouTube channel entries and download public videos locally."""

    _MAX_DOWNLOAD_HEIGHT = 720
    _NETWORK_RETRY_ATTEMPTS = 3
    _TRANSIENT_NETWORK_MARKERS = (
        "unable to download api page",
        "eof occurred in violation of protocol",
        "sslerror",
        "connection reset",
        "connection aborted",
        "temporarily unavailable",
        "timed out",
    )

    @staticmethod
    def is_youtube_url(value: str) -> bool:
        host = urlsplit(str(value or "").strip()).netloc.lower()
        return host == "youtu.be" or host.endswith("youtube.com") or host.endswith("youtube-nocookie.com")

    @classmethod
    def normalize_channel_url(cls, value: str) -> str:
        raw_url = str(value or "").strip()
        if not raw_url:
            raise YouTubeServiceError("请输入 YouTube 频道链接。")
        if not cls.is_youtube_url(raw_url):
            raise YouTubeServiceError("这不是有效的 YouTube 频道链接。")

        parts = urlsplit(raw_url)
        path = parts.path.rstrip("/")
        if not path:
            raise YouTubeServiceError("请粘贴频道主页、@频道名或频道视频页链接。")
        if path == "/watch" or path.startswith("/shorts/"):
            raise YouTubeServiceError("这里需要 YouTube 频道链接，不是单条视频链接。")
        if path.endswith("/videos"):
            videos_path = path
        elif path.endswith(("/shorts", "/streams", "/playlists", "/featured")):
            videos_path = path.rsplit("/", 1)[0] + "/videos"
        else:
            videos_path = f"{path}/videos"
        return urlunsplit(("https", "www.youtube.com", videos_path, "", ""))

    @staticmethod
    def _require_yt_dlp():
        try:
            from yt_dlp import YoutubeDL
        except ImportError as exc:  # pragma: no cover - depends on deployment dependencies
            raise YouTubeServiceError("缺少 YouTube 下载组件，请重新安装或更新素材分析助手。") from exc
        return YoutubeDL

    @staticmethod
    def _apply_synced_cookies(options: dict[str, object]) -> None:
        if not YOUTUBE_COOKIES_PATH.is_file() or YOUTUBE_COOKIES_PATH.stat().st_size <= 0:
            return
        options["cookiefile"] = str(YOUTUBE_COOKIES_PATH)
        LOGGER.info("Using locally synchronized YouTube browser cookies.")

    @staticmethod
    def _apply_synced_cookies_to_command(command: list[str]) -> bool:
        if not YOUTUBE_COOKIES_PATH.is_file() or YOUTUBE_COOKIES_PATH.stat().st_size <= 0:
            return False
        command.extend(["--cookies", str(YOUTUBE_COOKIES_PATH)])
        return True

    @staticmethod
    def _ffmpeg_proxy_from_environment() -> str | None:
        """Return an HTTP proxy that ffmpeg can use for range downloads."""
        try:
            configured = YOUTUBE_PROXY_CONFIG_PATH.read_text(encoding="utf-8").strip()
        except OSError:
            configured = ""
        if configured.lower().startswith(("http://", "https://")):
            return configured
        for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            value = str(os.environ.get(name) or "").strip()
            if value.lower().startswith(("http://", "https://")):
                return value
        return None

    @classmethod
    def _is_transient_network_error(cls, exc: Exception) -> bool:
        message = str(exc).lower()
        return any(marker in message for marker in cls._TRANSIENT_NETWORK_MARKERS)

    def _extract_info_with_retry(
        self,
        options: dict[str, object],
        source_url: str,
        *,
        download: bool,
        should_cancel: Callable[[], bool] | None = None,
    ) -> dict:
        YoutubeDL = self._require_yt_dlp()
        for attempt in range(1, self._NETWORK_RETRY_ATTEMPTS + 1):
            if should_cancel is not None and should_cancel():
                raise YouTubeDownloadCancelled("操作已取消。")
            try:
                with YoutubeDL(options) as downloader:
                    payload = downloader.extract_info(source_url, download=download)
                return payload if isinstance(payload, dict) else {}
            except YouTubeDownloadCancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                if attempt >= self._NETWORK_RETRY_ATTEMPTS or not self._is_transient_network_error(exc):
                    raise
                delay_seconds = attempt * 2
                LOGGER.warning(
                    "YouTube transient network error. Retrying %s/%s in %ss. url=%s error=%s",
                    attempt,
                    self._NETWORK_RETRY_ATTEMPTS,
                    delay_seconds,
                    source_url,
                    exc,
                )
                if should_cancel is not None and should_cancel():
                    raise YouTubeDownloadCancelled("操作已取消。")
                time.sleep(delay_seconds)
        raise YouTubeServiceError("YouTube 视频解析重试结束后仍未返回结果。")

    @staticmethod
    def _format_video_url(video_id: str, fallback_url: str = "") -> str:
        clean_id = str(video_id or "").strip()
        if clean_id:
            return f"https://www.youtube.com/watch?v={clean_id}"
        return str(fallback_url or "").strip()

    @classmethod
    def _to_collected_video(
        cls,
        entry: dict,
        *,
        default_channel: str | None,
        default_channel_id: str | None,
    ) -> YouTubeCollectedVideo | None:
        if not isinstance(entry, dict):
            return None
        video_id = str(entry.get("id") or "").strip()
        share_url = cls._format_video_url(video_id, str(entry.get("url") or ""))
        if not share_url:
            return None
        title = str(entry.get("title") or video_id or share_url).strip()
        duration = entry.get("duration")
        try:
            duration_seconds = max(0, int(duration)) if duration is not None else None
        except (TypeError, ValueError):
            duration_seconds = None
        view_count = entry.get("view_count")
        try:
            normalized_view_count = max(0, int(view_count)) if view_count is not None else None
        except (TypeError, ValueError):
            normalized_view_count = None
        return YouTubeCollectedVideo(
            video_id=video_id,
            share_url=share_url,
            title=title,
            channel=str(entry.get("channel") or entry.get("uploader") or default_channel or "").strip() or None,
            channel_id=str(entry.get("channel_id") or default_channel_id or "").strip() or None,
            duration_seconds=duration_seconds,
            upload_date=str(entry.get("upload_date") or "").strip() or None,
            view_count=normalized_view_count,
            thumbnail_url=str(entry.get("thumbnail") or "").strip() or None,
        )

    def collect_channel(
        self,
        source_url: str,
        *,
        max_items: int = 0,
        progress_callback: Callable[[str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> YouTubeCollectionResult:
        normalized_url = self.normalize_channel_url(source_url)
        if should_cancel is not None and should_cancel():
            raise YouTubeDownloadCancelled("采集已取消。")
        if progress_callback is not None:
            progress_callback("正在读取 YouTube 频道公开视频目录...")

        options: dict[str, object] = {
            "extract_flat": "in_playlist",
            "skip_download": True,
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": True,
        }
        if max_items > 0:
            options["playlistend"] = max_items
        self._apply_synced_cookies(options)
        try:
            payload = self._extract_info_with_retry(
                options,
                normalized_url,
                download=False,
                should_cancel=should_cancel,
            )
        except YouTubeDownloadCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            raise YouTubeServiceError(f"读取 YouTube 频道失败: {exc}") from exc

        if should_cancel is not None and should_cancel():
            raise YouTubeDownloadCancelled("采集已取消。")
        if not isinstance(payload, dict):
            raise YouTubeServiceError("频道没有返回可用的视频目录。")

        channel = str(payload.get("channel") or payload.get("uploader") or payload.get("title") or "").strip() or None
        channel_id = str(payload.get("channel_id") or payload.get("uploader_id") or "").strip() or None
        seen_urls: set[str] = set()
        videos: list[YouTubeCollectedVideo] = []
        for raw_entry in payload.get("entries") or []:
            if should_cancel is not None and should_cancel():
                raise YouTubeDownloadCancelled("采集已取消。")
            item = self._to_collected_video(
                raw_entry,
                default_channel=channel,
                default_channel_id=channel_id,
            )
            if item is None or item.share_url in seen_urls:
                continue
            seen_urls.add(item.share_url)
            videos.append(item)

        if progress_callback is not None:
            progress_callback(f"已识别 {len(videos)} 条公开视频。")
        LOGGER.info("YouTube channel collected. url=%s channel=%s count=%s", normalized_url, channel, len(videos))
        return YouTubeCollectionResult(
            source_url=normalized_url,
            channel=channel,
            channel_id=channel_id,
            videos=videos,
        )

    def download_video(
        self,
        source_url: str,
        *,
        max_duration_seconds: int | None = None,
        fast_range_download: bool = False,
        progress_callback: Callable[[int, int], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> YouTubeDownloadResult:
        if not self.is_youtube_url(source_url):
            raise YouTubeServiceError("这不是有效的 YouTube 视频链接。")
        parts = urlsplit(source_url)
        is_short_url = parts.netloc.lower() == "youtu.be" and bool(parts.path.strip("/"))
        is_watch_url = parts.path == "/watch" and bool(parse_qs(parts.query).get("v"))
        is_short_url_path = parts.path.startswith("/shorts/") and bool(parts.path.removeprefix("/shorts/").strip("/"))
        if not (is_short_url or is_watch_url or is_short_url_path):
            raise YouTubeServiceError("频道链接请先通过“ YouTube 频道采集”导入视频，再下载具体视频。")

        output_path = build_download_output_path("youtube_video", suffix=".mp4")
        output_template = str(output_path.with_suffix(".%(ext)s"))

        def check_cancelled() -> None:
            if should_cancel is not None and should_cancel():
                raise YouTubeDownloadCancelled("下载已取消。")

        def handle_progress(status: dict) -> None:
            check_cancelled()
            if progress_callback is None:
                return
            state = str(status.get("status") or "")
            if state == "downloading":
                downloaded = int(status.get("downloaded_bytes") or 0)
                total = int(status.get("total_bytes") or status.get("total_bytes_estimate") or 0)
                progress_callback(downloaded, total)
            elif state == "finished":
                total = int(status.get("total_bytes") or status.get("downloaded_bytes") or 0)
                progress_callback(total, total)

        options: dict[str, object] = {
            "format": (
                f"bv*[height<={self._MAX_DOWNLOAD_HEIGHT}][ext=mp4]+ba[ext=m4a]/"
                f"b[height<={self._MAX_DOWNLOAD_HEIGHT}][ext=mp4]/bv*+ba/b"
            ),
            "outtmpl": output_template,
            "merge_output_format": "mp4",
            "ffmpeg_location": str(FFMPEG_DIR),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "continuedl": True,
            "progress_hooks": [handle_progress],
            # The public Android VR client avoids the page-refresh response observed on desktop player requests.
            "extractor_args": {"youtube": {"player_client": ["android_vr"]}},
        }
        try:
            duration_limit = max(0, int(max_duration_seconds or 0))
        except (TypeError, ValueError):
            duration_limit = 0
        if duration_limit > 0:
            options["download_ranges"] = lambda _info, _downloader: [
                {"start_time": 0, "end_time": duration_limit}
            ]
            if not fast_range_download:
                options["force_keyframes_at_cuts"] = True
            ffmpeg_proxy = self._ffmpeg_proxy_from_environment()
            if ffmpeg_proxy:
                options["proxy"] = ffmpeg_proxy
                options["external_downloader_args"] = {
                    "ffmpeg_i1": ["-http_proxy", ffmpeg_proxy],
                    "ffmpeg_i2": ["-http_proxy", ffmpeg_proxy],
                }
                LOGGER.info("Using configured network proxy for YouTube range download.")
        self._apply_synced_cookies(options)
        check_cancelled()
        try:
            payload = self._extract_info_with_retry(
                options,
                source_url,
                download=True,
                should_cancel=should_cancel,
            )
        except YouTubeDownloadCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            message = str(exc)
            lowered = message.lower()
            LOGGER.warning("YouTube download failed. source=%s error=%s", source_url, message)
            if self._can_use_compat_downloader() and "requested format is not available" in lowered:
                LOGGER.info("Falling back to yt-dlp nightly for unavailable Python yt-dlp format.")
                return self._download_video_with_compat_tool(
                    source_url,
                    output_path=output_path,
                    max_duration_seconds=duration_limit,
                    should_cancel=should_cancel,
                )
            if "sign in to confirm" in lowered and "not a bot" in lowered:
                raise YouTubeServiceError(
                    "YouTube 要求登录校验。请打开 YouTube 频道采集页的内置浏览器登录账号，"
                    "再点击“同步登录状态”后重试下载。"
                ) from exc
            if self._is_transient_network_error(exc):
                raise YouTubeServiceError(
                    "YouTube 播放器连接连续重试后仍失败，请检查网络或代理后重试。"
                ) from exc
            raise YouTubeServiceError(f"下载 YouTube 视频失败: {exc}") from exc
        check_cancelled()

        if not output_path.exists():
            raise YouTubeServiceError("YouTube 下载完成后没有找到合并的视频文件。")
        title = str((payload or {}).get("title") or "").strip() or None
        author = str((payload or {}).get("uploader") or (payload or {}).get("channel") or "").strip() or None
        resolved_url = str((payload or {}).get("webpage_url") or source_url).strip()
        media_url = str((payload or {}).get("url") or "").strip() or None
        LOGGER.info("YouTube download completed. source=%s path=%s", source_url, output_path)
        return YouTubeDownloadResult(
            share_url=source_url,
            resolved_url=resolved_url,
            local_path=str(output_path),
            title=title,
            author=author,
            media_url=media_url,
        )

    @staticmethod
    def _can_use_compat_downloader() -> bool:
        return YOUTUBE_COMPAT_YTDLP_PATH.is_file() and YOUTUBE_COMPAT_NODE_PATH.is_file()

    def _download_video_with_compat_tool(
        self,
        source_url: str,
        *,
        output_path: Path,
        max_duration_seconds: int,
        should_cancel: Callable[[], bool] | None,
    ) -> YouTubeDownloadResult:
        """Use the bundled nightly client when Python yt-dlp misses a valid format."""
        if should_cancel is not None and should_cancel():
            raise YouTubeDownloadCancelled("下载已取消。")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        template = str(output_path.with_suffix(".%(ext)s"))
        command = [
            str(YOUTUBE_COMPAT_YTDLP_PATH),
            "--js-runtimes",
            "node",
            "--no-warnings",
            "--no-progress",
            "-f",
            # Range downloads should prefer a progressive stream.  Separate
            # video/audio streams can make ffmpeg wait for the full source.
            (
                f"b[height<=360]/b[height<={self._MAX_DOWNLOAD_HEIGHT}]/b"
                if max_duration_seconds > 0
                else f"bv*[height<={self._MAX_DOWNLOAD_HEIGHT}]+ba/b[height<={self._MAX_DOWNLOAD_HEIGHT}]/b"
            ),
            "--merge-output-format",
            "mp4",
            "-o",
            template,
        ]
        if max_duration_seconds > 0:
            command.extend(["--download-sections", f"*0-{max_duration_seconds}"])
        proxy = self._ffmpeg_proxy_from_environment()
        if proxy:
            command.extend(["--proxy", proxy])
        self._apply_synced_cookies_to_command(command)
        command.append(source_url)
        environment = os.environ.copy()
        environment["PATH"] = f"{YOUTUBE_COMPAT_NODE_PATH.parent}{os.pathsep}{environment.get('PATH', '')}"
        if proxy:
            for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
                environment[name] = proxy
        process = subprocess.Popen(
            command,
            # This route does not consume process output. Do not expose pipes
            # to ffmpeg/Node descendants, or cleanup can wait on inherited
            # handles after yt-dlp has already exited.
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            **_hidden_process_kwargs(),
        )
        while process.poll() is None:
            if should_cancel is not None and should_cancel():
                self._terminate_process_tree(process)
                raise YouTubeDownloadCancelled("下载已取消。")
            time.sleep(0.2)
        _stdout, stderr = process.communicate()
        if process.returncode != 0:
            raise YouTubeServiceError("yt-dlp nightly 下载失败。")
        candidates = [
            path
            for path in output_path.parent.glob(f"{output_path.stem}.*")
            if path.is_file() and path.suffix.lower() in {".mp4", ".mkv", ".webm"}
        ]
        if not candidates:
            raise YouTubeServiceError("yt-dlp nightly 下载完成后没有找到视频文件。")
        final_path = max(candidates, key=lambda path: path.stat().st_mtime)
        LOGGER.info("YouTube video completed through yt-dlp nightly. source=%s path=%s", source_url, final_path)
        return YouTubeDownloadResult(
            share_url=source_url,
            resolved_url=source_url,
            local_path=str(final_path),
        )

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen) -> None:
        """Stop yt-dlp and ffmpeg children together on Windows cancellation."""
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                # Terminating timed-out compatibility workers must not flash a
                # console window for each segment.
                **_hidden_process_kwargs(),
            )
            # taskkill returns before Python's process handle is necessarily
            # reaped. Wait here so callers never start a replacement process
            # while the old yt-dlp/Node/ffmpeg tree is still shutting down.
            try:
                process.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                LOGGER.warning("Compatibility process did not exit after taskkill. pid=%s", process.pid)
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


__all__ = [
    "YouTubeCollectedVideo",
    "YouTubeCollectionResult",
    "YouTubeDownloadCancelled",
    "YouTubeDownloadResult",
    "YouTubeService",
    "YouTubeServiceError",
]
