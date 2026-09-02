from __future__ import annotations

import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit

from app.models import YouTubeVideo
from app.settings import YOUTUBE_COOKIES_PATH


class YouTubeCollectionError(RuntimeError):
    pass


class YouTubeCollector:
    """A focused copy of the existing project's yt-dlp public-channel collector."""

    _RETRYABLE_MARKERS = (
        "unable to download api page",
        "eof occurred in violation of protocol",
        "connection reset",
        "connection aborted",
        "temporarily unavailable",
        "timed out",
    )

    @staticmethod
    def normalize_channel_url(value: str) -> str:
        raw_url = str(value or "").strip()
        parts = urlsplit(raw_url)
        host = parts.netloc.lower()
        if not raw_url or not (host == "youtu.be" or host.endswith("youtube.com")):
            raise YouTubeCollectionError("请输入 YouTube 频道链接。")
        path = parts.path.rstrip("/")
        if not path or path == "/watch" or path.startswith("/shorts/"):
            raise YouTubeCollectionError("请输入 YouTube 频道链接，不能使用单视频链接。")
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
        except ImportError as exc:  # pragma: no cover
            raise YouTubeCollectionError("yt-dlp is not installed.") from exc
        return YoutubeDL

    @staticmethod
    def _with_cookies(options: dict[str, object]) -> dict[str, object]:
        if YOUTUBE_COOKIES_PATH.is_file() and YOUTUBE_COOKIES_PATH.stat().st_size > 0:
            options["cookiefile"] = str(YOUTUBE_COOKIES_PATH)
        return options

    def _extract(self, options: dict[str, object], url: str, *, download: bool) -> dict:
        YoutubeDL = self._require_yt_dlp()
        last_error: Exception | None = None
        for attempt in range(1, 4):
            try:
                with YoutubeDL(self._with_cookies(options)) as downloader:
                    payload = downloader.extract_info(url, download=download)
                return payload if isinstance(payload, dict) else {}
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt == 3 or not any(token in str(exc).lower() for token in self._RETRYABLE_MARKERS):
                    break
                time.sleep(attempt * 2)
        raise YouTubeCollectionError(f"YouTube request failed: {last_error}") from last_error

    def collect_channel(self, source_url: str, *, max_items: int = 0) -> list[YouTubeVideo]:
        normalized_url = self.normalize_channel_url(source_url)
        options: dict[str, object] = {
            # Channel discovery only needs entry metadata.  Fully flatten the
            # playlist so an unavailable playback format cannot abort the list.
            "extract_flat": True,
            "skip_download": True,
            "ignore_no_formats_error": True,
            "noplaylist": False,
            "quiet": True,
            "no_warnings": True,
            "extractor_args": {"youtube": {"player_client": ["android_vr"]}},
        }
        if max(0, int(max_items or 0)):
            options["playlistend"] = max(0, int(max_items or 0))
        payload = self._extract(options, normalized_url, download=False)
        channel_id = str(payload.get("channel_id") or payload.get("uploader_id") or "").strip()
        seen_ids: set[str] = set()
        videos: list[YouTubeVideo] = []
        for entry in payload.get("entries") or []:
            if not isinstance(entry, dict):
                continue
            video_id = str(entry.get("id") or "").strip()
            if not video_id or video_id in seen_ids:
                continue
            seen_ids.add(video_id)
            videos.append(
                YouTubeVideo(
                    video_id=video_id,
                    source_url=f"https://www.youtube.com/watch?v={video_id}",
                    title=str(entry.get("title") or "").strip() or video_id,
                    channel=str(entry.get("channel") or payload.get("channel") or "").strip(),
                    upload_date=str(entry.get("upload_date") or "").strip(),
                    duration_seconds=max(0, int(entry.get("duration") or 0)),
                    # Flat playlist metadata normally contains the selected
                    # thumbnail.  The standard URL is a reliable fallback.
                    thumbnail_url=str(entry.get("thumbnail") or "").strip()
                    or f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
                    channel_id=str(entry.get("channel_id") or channel_id).strip(),
                )
            )
        return videos

    @staticmethod
    def is_video_url(value: str) -> bool:
        parts = urlsplit(str(value or "").strip())
        return bool(parts.netloc and (parse_qs(parts.query).get("v") or parts.path.startswith("/shorts/")))

    @staticmethod
    def srt_cues(srt_text: str) -> list[tuple[float, float, str]]:
        cues: list[tuple[float, float, str]] = []
        blocks = re.split(r"\r?\n\s*\r?\n", srt_text.strip())
        for block in blocks:
            lines = [line.strip() for line in block.splitlines() if line.strip()]
            timing_index = next((index for index, line in enumerate(lines) if "-->" in line), -1)
            if timing_index < 0:
                continue
            try:
                start_raw, end_raw = (part.strip() for part in lines[timing_index].split("-->", 1))
                start = YouTubeCollector._srt_time_to_seconds(start_raw)
                end = YouTubeCollector._srt_time_to_seconds(end_raw.split()[0])
            except (TypeError, ValueError):
                continue
            text = " ".join(lines[timing_index + 1 :])
            if text:
                cues.append((start, end, re.sub(r"<[^>]+>", "", text)))
        return cues

    @staticmethod
    def _srt_time_to_seconds(value: str) -> float:
        hours, minutes, seconds = value.replace(",", ".").split(":")
        return int(hours) * 3600 + int(minutes) * 60 + float(seconds)
