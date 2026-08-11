from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from urllib.parse import urlparse

import requests

from app.config.settings import YOUTUBE_PROXY_CONFIG_PATH
from app.models import YouTubeVideo
from app.settings import COVER_DIR
from app.task_control import TaskControl
from app.utils.logger import get_logger


LOGGER = get_logger(__name__)


@dataclass(frozen=True)
class CoverDownloadResult:
    video_id: str
    path: str = ""
    error: str = ""


class YouTubeCoverService:
    """Downloads public YouTube thumbnails without downloading video media."""

    # Thumbnail files are tiny. Long retry waits make batch progress look frozen
    # without improving success rate, so fail over to the next image URL quickly.
    _TIMEOUT_SECONDS = (5, 8)
    _MAX_ATTEMPTS = 2
    _DEFAULT_BATCH_CONCURRENCY = 4
    _VALID_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}

    @staticmethod
    def _proxy_settings() -> dict[str, str] | None:
        try:
            proxy = YOUTUBE_PROXY_CONFIG_PATH.read_text(encoding="utf-8").strip()
        except OSError:
            proxy = ""
        return {"http": proxy, "https": proxy} if proxy else None

    @classmethod
    def _candidate_urls(cls, video: YouTubeVideo) -> list[str]:
        standard_base = f"https://i.ytimg.com/vi/{video.video_id}"
        candidates = [
            video.thumbnail_url.strip(),
            f"{standard_base}/maxresdefault.jpg",
            f"{standard_base}/hqdefault.jpg",
        ]
        return list(dict.fromkeys(url for url in candidates if url))

    @classmethod
    def _cached_path(cls, video_id: str, output_dir: Path) -> Path | None:
        for suffix in cls._VALID_SUFFIXES:
            candidate = output_dir / f"{video_id}{suffix}"
            if candidate.is_file() and candidate.stat().st_size >= 1024:
                return candidate
        return None

    @classmethod
    def _suffix_for(cls, response: requests.Response, url: str) -> str:
        content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
        suffix_by_type = {
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
        }
        if content_type in suffix_by_type:
            return suffix_by_type[content_type]
        suffix = Path(urlparse(url).path).suffix.lower()
        return suffix if suffix in cls._VALID_SUFFIXES else ".jpg"

    def download_cover(self, video: YouTubeVideo, *, output_dir: Path = COVER_DIR) -> CoverDownloadResult:
        started_at = monotonic()
        output_dir.mkdir(parents=True, exist_ok=True)
        cached = self._cached_path(video.video_id, output_dir)
        if cached is not None:
            LOGGER.info("Cover cache hit. video_id=%s", video.video_id)
            return CoverDownloadResult(video.video_id, str(cached))

        errors: list[str] = []
        session = requests.Session()
        session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/136 Safari/537.36"})
        try:
            for url in self._candidate_urls(video):
                for _attempt in range(self._MAX_ATTEMPTS):
                    try:
                        response = session.get(
                            url,
                            timeout=self._TIMEOUT_SECONDS,
                            proxies=self._proxy_settings(),
                        )
                        response.raise_for_status()
                        content_type = response.headers.get("content-type", "").lower()
                        if not content_type.startswith("image/") or len(response.content) < 1024:
                            raise RuntimeError("返回内容不是有效封面图片")
                        target = output_dir / f"{video.video_id}{self._suffix_for(response, url)}"
                        temporary = target.with_suffix(f"{target.suffix}.part")
                        temporary.write_bytes(response.content)
                        temporary.replace(target)
                        LOGGER.info(
                            "Cover downloaded. video_id=%s bytes=%s elapsed_ms=%s",
                            video.video_id,
                            len(response.content),
                            int((monotonic() - started_at) * 1000),
                        )
                        return CoverDownloadResult(video.video_id, str(target))
                    except (OSError, requests.RequestException, RuntimeError) as exc:
                        errors.append(str(exc))
        finally:
            session.close()
        error = errors[-1] if errors else "未找到可用封面"
        LOGGER.warning(
            "Cover download failed. video_id=%s attempts=%s elapsed_ms=%s error_type=%s",
            video.video_id,
            len(errors),
            int((monotonic() - started_at) * 1000),
            type(error).__name__,
        )
        return CoverDownloadResult(video.video_id, error=error)

    def download_batch(
        self,
        videos: list[YouTubeVideo],
        *,
        progress_callback=None,
        started_callback=None,
        task_control: TaskControl | None = None,
        concurrency: int = _DEFAULT_BATCH_CONCURRENCY,
    ) -> tuple[list[CoverDownloadResult], bool]:
        total = len(videos)
        if not total:
            return [], False
        limit = max(1, min(int(concurrency or self._DEFAULT_BATCH_CONCURRENCY), 6, total))
        results: list[CoverDownloadResult | None] = [None] * total
        pending: dict[object, tuple[int, YouTubeVideo]] = {}
        next_index = 0
        completed = 0
        cancelled = False

        def start_next(executor: ThreadPoolExecutor) -> bool:
            nonlocal next_index, cancelled
            if next_index >= total:
                return False
            if task_control is not None and not task_control.checkpoint():
                cancelled = True
                return False
            index = next_index
            video = videos[index]
            next_index += 1
            if started_callback is not None:
                started_callback(index + 1, total, video)
            pending[executor.submit(self.download_cover, video)] = (index, video)
            return True

        with ThreadPoolExecutor(max_workers=limit, thread_name_prefix="cover-download") as executor:
            while len(pending) < limit and start_next(executor):
                pass
            while pending:
                finished, _ = wait(tuple(pending), return_when=FIRST_COMPLETED)
                for future in finished:
                    index, video = pending.pop(future)
                    try:
                        result = future.result()
                    except Exception as exc:  # noqa: BLE001
                        LOGGER.exception("Unhandled cover download worker error. video_id=%s", video.video_id)
                        result = CoverDownloadResult(video.video_id, error=str(exc))
                    results[index] = result
                    completed += 1
                    if progress_callback is not None:
                        progress_callback(completed, total, video, result)
                while not cancelled and len(pending) < limit and start_next(executor):
                    pass
                if cancelled:
                    for future in pending:
                        future.cancel()

        return [result for result in results if result is not None], cancelled
