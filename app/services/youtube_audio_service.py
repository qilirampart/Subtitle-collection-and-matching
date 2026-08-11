from __future__ import annotations

import os
import json
import math
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Thread
from typing import Callable

import requests

from app.config.settings import (
    EXTRACTED_AUDIO_DIR,
    FFMPEG_DIR,
    YOUTUBE_COMPAT_NODE_PATH,
    YOUTUBE_COMPAT_YTDLP_PATH,
)
from app.services.youtube_service import YouTubeDownloadCancelled, YouTubeService, YouTubeServiceError
from app.utils.ffmpeg import _hidden_process_kwargs
from app.utils.logger import get_logger
from app.utils.paths import next_compact_name


LOGGER = get_logger(__name__)


@dataclass(frozen=True)
class YouTubeAudioDownloadResult:
    share_url: str
    local_path: str
    duration_seconds: int


class YouTubeAudioService:
    """Downloads a leading YouTube audio range in parallel, then joins AAC segments."""

    _SEGMENT_SECONDS = 60
    _COMPAT_SEGMENT_SECONDS = 30
    _COMPAT_MAX_WORKERS = 6
    _COMPAT_SEGMENT_TIMEOUT_SECONDS = 90
    _COMPAT_FIRST_OUTPUT_TIMEOUT_SECONDS = 40
    _COMPAT_MERGE_TIMEOUT_SECONDS = 60
    _DIRECT_RANGE_MAX_SECONDS = 300
    _DIRECT_RANGE_BUFFER_SECONDS = 12
    _DIRECT_METADATA_TIMEOUT_SECONDS = 45
    _DIRECT_ROUTE_FAILURE_LIMITS = {"configured-proxy": 2, "direct": 1}
    _DIRECT_ROUTE_COOLDOWN_SECONDS = {"configured-proxy": 90, "direct": 15 * 60}

    def __init__(self) -> None:
        self._youtube = YouTubeService()
        self._direct_route_failures: dict[str, int] = {}
        self._direct_route_cooldowns: dict[str, float] = {}

    def download_audio(
        self,
        source_url: str,
        *,
        max_duration_seconds: int = 300,
        concurrency: int = 1,
        progress_callback: Callable[[int, int], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> YouTubeAudioDownloadResult:
        requested_duration = max(0, int(max_duration_seconds or 0))
        duration = requested_duration or self._get_video_duration(source_url, should_cancel=should_cancel)
        if duration <= 0:
            raise YouTubeServiceError("Unable to determine YouTube audio duration.")
        if YOUTUBE_COMPAT_YTDLP_PATH.is_file() and YOUTUBE_COMPAT_NODE_PATH.is_file():
            # Range download avoids ffmpeg's slow remote time-range extraction.
            # Keep the established segmented route as a compatibility fallback.
            if 0 < requested_duration <= self._DIRECT_RANGE_MAX_SECONDS:
                try:
                    return self._download_audio_with_direct_range(
                        source_url,
                        duration=duration,
                        requested_duration=requested_duration,
                        progress_callback=progress_callback,
                        should_cancel=should_cancel,
                    )
                except YouTubeDownloadCancelled:
                    raise
                except Exception as exc:  # noqa: BLE001
                    LOGGER.info("Direct YouTube audio range download unavailable; using segmented fallback. reason=%s", type(exc).__name__)
            return self._download_audio_with_compat_tool(
                source_url,
                duration=duration,
                concurrency=concurrency,
                progress_callback=progress_callback,
                should_cancel=should_cancel,
            )
        workers = max(1, min(int(concurrency or 1), 3))
        output_dir = EXTRACTED_AUDIO_DIR / next_compact_name("youtube_audio")
        output_dir.mkdir(parents=True, exist_ok=True)
        segments = [(start, min(start + self._SEGMENT_SECONDS, duration)) for start in range(0, duration, self._SEGMENT_SECONDS)]

        def download_segment(index: int, start: int, end: int) -> Path:
            if should_cancel is not None and should_cancel():
                raise YouTubeDownloadCancelled("Audio download cancelled.")
            path = output_dir / f"segment_{index:03d}.m4a"
            options: dict[str, object] = {
                "format": "ba[ext=m4a]/ba",
                "outtmpl": str(path.with_suffix(".%(ext)s")),
                "ffmpeg_location": str(FFMPEG_DIR),
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "extractor_args": {"youtube": {"player_client": ["android_vr"]}},
                "download_ranges": lambda _info, _downloader: [{"start_time": start, "end_time": end}],
            }
            proxy = self._youtube._ffmpeg_proxy_from_environment()
            if proxy:
                # yt-dlp resolves the player response and ffmpeg fetches the media
                # stream separately, so both layers must receive the proxy.
                options["proxy"] = proxy
                options["external_downloader_args"] = {"ffmpeg_i1": ["-http_proxy", proxy]}
                LOGGER.info("Using configured proxy for YouTube audio segment. proxy=%s", proxy)
            self._youtube._apply_synced_cookies(options)
            self._youtube._extract_info_with_retry(options, source_url, download=True, should_cancel=should_cancel)
            if not path.exists():
                raise YouTubeServiceError("YouTube audio segment was not created.")
            return path

        completed = 0
        paths: list[Path | None] = [None] * len(segments)
        try:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="youtube-audio") as executor:
                futures = {
                    executor.submit(download_segment, index, start, end): index
                    for index, (start, end) in enumerate(segments)
                }
                for future in as_completed(futures):
                    if should_cancel is not None and should_cancel():
                        for pending in futures:
                            pending.cancel()
                        raise YouTubeDownloadCancelled("Audio download cancelled.")
                    index = futures[future]
                    paths[index] = future.result()
                    completed += 1
                    if progress_callback is not None:
                        progress_callback(completed, len(segments))

            final_path = EXTRACTED_AUDIO_DIR / f"{next_compact_name('youtube_audio')}.m4a"
            concat_path = output_dir / "concat.txt"
            concat_path.write_text(
                "".join(f"file '{path.as_posix()}'\n" for path in paths if path is not None),
                encoding="utf-8",
            )
            completed_process = subprocess.run(
                [str(FFMPEG_DIR / "ffmpeg.exe"), "-y", "-f", "concat", "-safe", "0", "-i", str(concat_path), "-c", "copy", str(final_path)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                **_hidden_process_kwargs(),
            )
            if completed_process.returncode != 0:
                raise YouTubeServiceError(completed_process.stderr.strip() or "Failed to merge YouTube audio segments.")
            return YouTubeAudioDownloadResult(source_url, str(final_path), duration)
        finally:
            shutil.rmtree(output_dir, ignore_errors=True)

    def _download_audio_with_direct_range(
        self,
        source_url: str,
        *,
        duration: int,
        requested_duration: int,
        progress_callback: Callable[[int, int], None] | None,
        should_cancel: Callable[[], bool] | None,
    ) -> YouTubeAudioDownloadResult:
        """Download only the leading audio bytes using yt-dlp's signed media URL."""
        if should_cancel is not None and should_cancel():
            raise YouTubeDownloadCancelled("Audio download cancelled.")

        proxy = self._youtube._ffmpeg_proxy_from_environment()
        metadata = self._resolve_direct_range_metadata(source_url, proxy, should_cancel=should_cancel)

        media_url = str(metadata.get("url") or "").strip()
        total_bytes = self._positive_int(metadata.get("filesize"))
        source_duration = self._positive_int(metadata.get("duration")) or duration
        if not media_url or total_bytes <= 0 or source_duration <= 0:
            raise YouTubeServiceError("Direct YouTube audio metadata is incomplete.")

        target_seconds = min(source_duration, requested_duration + self._DIRECT_RANGE_BUFFER_SECONDS)
        target_bytes = min(total_bytes, max(128 * 1024, math.ceil(total_bytes * target_seconds / source_duration)))
        extension = str(metadata.get("ext") or "m4a").strip().lower() or "m4a"
        if not extension.isalnum():
            extension = "m4a"
        output_path = EXTRACTED_AUDIO_DIR / f"{next_compact_name('youtube_audio')}.{extension}"

        def refresh_request_values() -> tuple[str, dict[str, str]]:
            refreshed_metadata = self._resolve_direct_range_metadata(source_url, proxy, should_cancel=should_cancel)
            refreshed_url = str(refreshed_metadata.get("url") or "").strip()
            refreshed_total_bytes = self._positive_int(refreshed_metadata.get("filesize"))
            refreshed_duration = self._positive_int(refreshed_metadata.get("duration")) or duration
            if not refreshed_url or refreshed_total_bytes <= 0 or refreshed_duration <= 0:
                raise YouTubeServiceError("Refreshed direct YouTube audio metadata is incomplete.")
            refreshed_seconds = min(refreshed_duration, requested_duration + self._DIRECT_RANGE_BUFFER_SECONDS)
            refreshed_bytes = min(
                refreshed_total_bytes,
                max(128 * 1024, math.ceil(refreshed_total_bytes * refreshed_seconds / refreshed_duration)),
            )
            refreshed_headers = refreshed_metadata.get("http_headers")
            return refreshed_url, {
                **(refreshed_headers if isinstance(refreshed_headers, dict) else {}),
                "Range": f"bytes=0-{refreshed_bytes - 1}",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/136.0 Safari/537.36",
            }

        metadata_headers = metadata.get("http_headers")
        headers = {
            **(metadata_headers if isinstance(metadata_headers, dict) else {}),
            "Range": f"bytes=0-{target_bytes - 1}",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/136.0 Safari/537.36",
        }

        # Some configured proxies return 403 for signed media Range requests.
        # Refresh the signed media URL once before recording the route failure.
        refreshed_proxy_metadata = False
        request_routes: list[tuple[str, dict[str, str] | None]] = []
        if proxy:
            request_routes.append(("configured-proxy", {"http": proxy, "https": proxy}))
        request_routes.append(("direct", None))
        request_routes = [
            route for route in request_routes
            if self._is_direct_route_available(route[0])
        ]
        if not request_routes:
            raise YouTubeServiceError("All direct audio Range routes are temporarily unavailable.")
        EXTRACTED_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        last_error: Exception | None = None
        try:
            for route_name, request_proxies in request_routes:
                session = requests.Session()
                try:
                    session.trust_env = False
                    media_started = time.monotonic()
                    downloaded_bytes = 0
                    with session.get(
                        media_url,
                        headers=headers,
                        proxies=request_proxies,
                        stream=True,
                        timeout=(3, 120),
                    ) as response:
                        if response.status_code != 206:
                            raise YouTubeServiceError(
                                f"Direct audio range request returned HTTP {response.status_code}."
                            )
                        with output_path.open("wb") as output_file:
                            for chunk in response.iter_content(chunk_size=256 * 1024):
                                if should_cancel is not None and should_cancel():
                                    raise YouTubeDownloadCancelled("Audio download cancelled.")
                                if chunk:
                                    output_file.write(chunk)
                                    downloaded_bytes += len(chunk)
                    media_elapsed = max(time.monotonic() - media_started, 0.001)
                    LOGGER.info(
                        "Direct YouTube audio Range request succeeded. route=%s bytes=%s elapsed=%.3fs speed=%.2fMiB/s",
                        route_name,
                        downloaded_bytes,
                        media_elapsed,
                        downloaded_bytes / media_elapsed / 1024 / 1024,
                    )
                    self._mark_direct_route_success(route_name)
                    last_error = None
                    break
                except YouTubeDownloadCancelled:
                    raise
                except Exception as exc:  # noqa: BLE001 - try the next route
                    last_error = exc
                    error_message = str(exc)
                    is_proxy_403 = (
                        route_name == "configured-proxy"
                        and error_message == "Direct audio range request returned HTTP 403."
                    )
                    if not error_message.startswith("Direct audio range request returned HTTP "):
                        error_message = type(exc).__name__
                    LOGGER.info(
                        "Direct YouTube audio Range request failed. route=%s elapsed=%.3fs error=%s",
                        route_name,
                        max(time.monotonic() - media_started, 0.001),
                        error_message,
                    )
                    output_path.unlink(missing_ok=True)
                    if is_proxy_403 and not refreshed_proxy_metadata:
                        refreshed_proxy_metadata = True
                        try:
                            media_url, headers = refresh_request_values()
                            LOGGER.info("Retrying configured-proxy Range request after refreshing media metadata.")
                            retry_started = time.monotonic()
                            retry_bytes = 0
                            with session.get(
                                media_url,
                                headers=headers,
                                proxies=request_proxies,
                                stream=True,
                                timeout=(3, 120),
                            ) as response:
                                if response.status_code != 206:
                                    raise YouTubeServiceError(
                                        f"Direct audio range request returned HTTP {response.status_code}."
                                    )
                                with output_path.open("wb") as output_file:
                                    for chunk in response.iter_content(chunk_size=256 * 1024):
                                        if should_cancel is not None and should_cancel():
                                            raise YouTubeDownloadCancelled("Audio download cancelled.")
                                        if chunk:
                                            output_file.write(chunk)
                                            retry_bytes += len(chunk)
                            retry_elapsed = max(time.monotonic() - retry_started, 0.001)
                            LOGGER.info(
                                "Direct YouTube audio Range retry succeeded. route=%s bytes=%s elapsed=%.3fs speed=%.2fMiB/s",
                                route_name,
                                retry_bytes,
                                retry_elapsed,
                                retry_bytes / retry_elapsed / 1024 / 1024,
                            )
                            self._mark_direct_route_success(route_name)
                            last_error = None
                            break
                        except YouTubeDownloadCancelled:
                            raise
                        except Exception as retry_exc:  # noqa: BLE001
                            output_path.unlink(missing_ok=True)
                            retry_error = str(retry_exc)
                            if not retry_error.startswith("Direct audio range request returned HTTP "):
                                retry_error = type(retry_exc).__name__
                            LOGGER.info("Refreshed configured-proxy Range retry failed. error=%s", retry_error)
                            last_error = retry_exc
                    self._mark_direct_route_failure(route_name)
                finally:
                    session.close()
                if last_error is None:
                    break
            if last_error is not None:
                raise last_error
            if output_path.stat().st_size < 128 * 1024:
                raise YouTubeServiceError("Direct audio range result is too small.")
            validation_started = time.monotonic()
            last_timestamp = self._read_last_audio_timestamp(output_path)
            validation_elapsed = max(time.monotonic() - validation_started, 0.001)
            LOGGER.info(
                "Direct YouTube audio Range validation completed. elapsed=%.3fs last_timestamp=%.3fs",
                validation_elapsed,
                last_timestamp,
            )
            if last_timestamp + 0.5 < requested_duration:
                raise YouTubeServiceError("Direct audio range result does not cover the requested duration.")
        except Exception:
            output_path.unlink(missing_ok=True)
            raise

        LOGGER.info(
            "Downloaded leading YouTube audio through direct byte range. requested_seconds=%s bytes=%s",
            requested_duration,
            output_path.stat().st_size,
        )
        if progress_callback is not None:
            progress_callback(1, 1)
        return YouTubeAudioDownloadResult(source_url, str(output_path), requested_duration)

    def _resolve_direct_range_metadata(
        self,
        source_url: str,
        proxy: str | None,
        *,
        should_cancel: Callable[[], bool] | None = None,
    ) -> dict[str, object]:
        command = [
            str(YOUTUBE_COMPAT_YTDLP_PATH),
            "--js-runtimes",
            "node",
            "--no-warnings",
            "--no-progress",
            "-J",
            "-f",
            "bestaudio[ext=m4a]/bestaudio/best",
        ]
        if proxy:
            command.extend(["--proxy", proxy])
        if self._youtube._apply_synced_cookies_to_command(command):
            LOGGER.info("Using locally synchronized YouTube browser cookies.")
        command.append(source_url)
        environment = os.environ.copy()
        environment["PATH"] = f"{YOUTUBE_COMPAT_NODE_PATH.parent}{os.pathsep}{environment.get('PATH', '')}"
        metadata_started = time.monotonic()
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            **_hidden_process_kwargs(),
        )
        output: dict[str, tuple[str, str]] = {}
        completed = Event()

        def collect_output() -> None:
            output["value"] = process.communicate()
            completed.set()

        # yt-dlp -J can emit a large player response. Drain its pipes in a
        # helper thread while the caller retains a cancellable timeout loop.
        collector_thread = Thread(target=collect_output, name="youtube-metadata-output", daemon=True)
        collector_thread.start()
        deadline = time.monotonic() + self._DIRECT_METADATA_TIMEOUT_SECONDS
        while not completed.wait(0.2):
            if should_cancel is not None and should_cancel():
                self._youtube._terminate_process_tree(process)  # noqa: SLF001
                completed.wait(5)
                raise YouTubeDownloadCancelled("Audio download cancelled.")
            if time.monotonic() >= deadline:
                self._youtube._terminate_process_tree(process)  # noqa: SLF001
                completed.wait(5)
                raise YouTubeServiceError("Direct YouTube audio metadata request timed out.")
        collector_thread.join(timeout=0.1)
        stdout, _stderr = output.get("value", ("", ""))
        metadata_elapsed = max(time.monotonic() - metadata_started, 0.001)
        if process.returncode != 0:
            LOGGER.info("Direct YouTube audio metadata failed. elapsed=%.3fs", metadata_elapsed)
            raise YouTubeServiceError("Unable to obtain direct YouTube audio metadata.")
        try:
            metadata = json.loads(stdout or "")
        except json.JSONDecodeError as exc:
            raise YouTubeServiceError("Unable to parse direct YouTube audio metadata.") from exc

        LOGGER.info(
            "Direct YouTube audio metadata resolved. elapsed=%.3fs duration_seconds=%s total_bytes=%s",
            metadata_elapsed,
            self._positive_int(metadata.get("duration")),
            self._positive_int(metadata.get("filesize")),
        )
        return metadata if isinstance(metadata, dict) else {}

    def _is_direct_route_available(self, route_name: str) -> bool:
        until = float(self._direct_route_cooldowns.get(route_name) or 0)
        if until <= time.monotonic():
            self._direct_route_cooldowns.pop(route_name, None)
            return True
        LOGGER.info("Skipping direct audio Range route during cooldown. route=%s", route_name)
        return False

    def _mark_direct_route_success(self, route_name: str) -> None:
        self._direct_route_failures.pop(route_name, None)
        self._direct_route_cooldowns.pop(route_name, None)

    def _mark_direct_route_failure(self, route_name: str) -> None:
        failures = int(self._direct_route_failures.get(route_name) or 0) + 1
        self._direct_route_failures[route_name] = failures
        limit = int(self._DIRECT_ROUTE_FAILURE_LIMITS.get(route_name) or 1)
        if failures < limit:
            return
        cooldown = int(self._DIRECT_ROUTE_COOLDOWN_SECONDS.get(route_name) or 60)
        self._direct_route_cooldowns[route_name] = time.monotonic() + cooldown
        LOGGER.info(
            "Direct audio Range route entered cooldown. route=%s failures=%s cooldown_seconds=%s",
            route_name,
            failures,
            cooldown,
        )

    @staticmethod
    def _positive_int(value: object) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _read_last_audio_timestamp(path: Path) -> float:
        probe = FFMPEG_DIR / "ffprobe.exe"
        if not probe.is_file():
            raise YouTubeServiceError("ffprobe is unavailable for direct audio validation.")
        completed_process = subprocess.run(
            [
                str(probe),
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "packet=pts_time",
                "-of",
                "csv=p=0",
                str(path),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            **_hidden_process_kwargs(),
        )
        if completed_process.returncode != 0:
            raise YouTubeServiceError("ffprobe could not read direct audio data.")
        for value in reversed(completed_process.stdout.splitlines()):
            try:
                return float(value.strip())
            except ValueError:
                continue
        raise YouTubeServiceError("Direct audio data contains no decodable packets.")

    def _download_audio_with_compat_tool(
        self,
        source_url: str,
        *,
        duration: int,
        concurrency: int,
        progress_callback: Callable[[int, int], None] | None,
        should_cancel: Callable[[], bool] | None,
    ) -> YouTubeAudioDownloadResult:
        """Use yt-dlp nightly's JS challenge solver with parallel audio ranges."""
        workers = max(1, min(int(concurrency or 1), self._COMPAT_MAX_WORKERS))
        download_started = time.monotonic()
        output_dir = EXTRACTED_AUDIO_DIR / next_compact_name("youtube_audio")
        output_dir.mkdir(parents=True, exist_ok=True)
        segments = [
            (start, min(start + self._COMPAT_SEGMENT_SECONDS, duration))
            for start in range(0, duration, self._COMPAT_SEGMENT_SECONDS)
        ]
        LOGGER.info(
            "Compatible YouTube audio fallback started. segments=%s workers=%s duration_seconds=%s segment_timeout_seconds=%s",
            len(segments),
            workers,
            duration,
            self._COMPAT_SEGMENT_TIMEOUT_SECONDS,
        )

        abort_event = Event()

        def should_stop() -> bool:
            return abort_event.is_set() or (should_cancel is not None and should_cancel())

        def download_segment(index: int, start: int, end: int) -> Path:
            if should_stop():
                raise YouTubeDownloadCancelled("Audio download cancelled.")
            template = output_dir / f"segment_{index:03d}.%(ext)s"
            command = [
                str(YOUTUBE_COMPAT_YTDLP_PATH),
                "--js-runtimes",
                "node",
                "--no-warnings",
                "--no-progress",
                "-f",
                # Format code 140 (M4A) is absent on some videos. Prefer M4A, but
                # fall back to any available audio-only stream instead of failing.
                "bestaudio[ext=m4a]/bestaudio/best",
                "--download-sections",
                f"*{start}-{end}",
                "-o",
                str(template),
            ]
            proxy = self._youtube._ffmpeg_proxy_from_environment()
            if proxy:
                command.extend(["--proxy", proxy])
                # --download-sections delegates the actual media transfer to ffmpeg.
                # yt-dlp's --proxy covers metadata requests, while ffmpeg reads these
                # environment variables for the signed googlevideo media URL.
                LOGGER.info("Using configured proxy for YouTube audio metadata and media transfer.")
            if self._youtube._apply_synced_cookies_to_command(command):
                LOGGER.info("Using locally synchronized YouTube browser cookies.")
            command.append(source_url)
            environment = os.environ.copy()
            environment["PATH"] = f"{YOUTUBE_COMPAT_NODE_PATH.parent}{os.pathsep}{environment.get('PATH', '')}"
            if proxy:
                for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
                    environment[key] = proxy
            segment_started = time.monotonic()
            returncode, stdout, stderr = self._run_compat_process(
                command,
                env=environment,
                should_cancel=should_stop,
                timeout_seconds=self._COMPAT_SEGMENT_TIMEOUT_SECONDS,
                description=f"Compatible YouTube audio segment {index + 1}/{len(segments)}",
                activity_probe=lambda: self._compat_segment_bytes(output_dir, index),
            )
            if returncode != 0:
                message = stderr.strip() or stdout.strip()
                LOGGER.warning(
                    "Compatible YouTube audio process failed. task=segment %s/%s exit_code=%s diagnostics=%s",
                    index + 1,
                    len(segments),
                    returncode,
                    self._compact_process_diagnostics(message),
                )
                raise YouTubeServiceError(message or "Compatible YouTube audio download failed.")
            candidates = [
                path for path in output_dir.glob(f"segment_{index:03d}.*")
                if path.is_file() and path.suffix.lower() in {".m4a", ".webm"}
            ]
            if not candidates:
                raise YouTubeServiceError("Compatible YouTube audio segment was not created.")
            result = max(candidates, key=lambda path: path.stat().st_size)
            LOGGER.info(
                "Compatible YouTube audio segment completed. index=%s range=%s-%s elapsed=%.3fs bytes=%s",
                index,
                start,
                end,
                max(time.monotonic() - segment_started, 0.001),
                result.stat().st_size,
            )
            return result

        completed = 0
        paths: list[Path | None] = [None] * len(segments)
        try:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="youtube-audio-fast") as executor:
                futures = {
                    executor.submit(download_segment, index, start, end): index
                    for index, (start, end) in enumerate(segments)
                }
                for future in as_completed(futures):
                    if should_stop():
                        abort_event.set()
                        for pending in futures:
                            pending.cancel()
                        raise YouTubeDownloadCancelled("Audio download cancelled.")
                    index = futures[future]
                    try:
                        paths[index] = future.result()
                    except Exception:
                        # Stop sibling yt-dlp/ffmpeg processes instead of waiting for
                        # their individual network timeouts after one segment fails.
                        abort_event.set()
                        for pending in futures:
                            pending.cancel()
                        raise
                    completed += 1
                    if progress_callback is not None:
                        progress_callback(completed, len(segments))

            first_path = next((path for path in paths if path is not None), None)
            suffix = first_path.suffix.lower() if first_path is not None else ".m4a"
            final_path = EXTRACTED_AUDIO_DIR / f"{next_compact_name('youtube_audio')}{suffix}"
            concat_path = output_dir / "concat.txt"
            concat_path.write_text(
                "".join(f"file '{path.as_posix()}'\n" for path in paths if path is not None),
                encoding="utf-8",
            )
            try:
                completed_process = subprocess.run(
                    [str(FFMPEG_DIR / "ffmpeg.exe"), "-y", "-f", "concat", "-safe", "0", "-i", str(concat_path), "-c", "copy", str(final_path)],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    check=False,
                    timeout=self._COMPAT_MERGE_TIMEOUT_SECONDS,
                    **_hidden_process_kwargs(),
                )
            except subprocess.TimeoutExpired as exc:
                raise YouTubeServiceError("Compatible YouTube audio merge timed out.") from exc
            if completed_process.returncode != 0:
                raise YouTubeServiceError(completed_process.stderr.strip() or "Failed to merge YouTube audio segments.")
            LOGGER.info(
                "Compatible YouTube audio fallback completed. segments=%s elapsed=%.3fs",
                len(segments),
                max(time.monotonic() - download_started, 0.001),
            )
            return YouTubeAudioDownloadResult(source_url, str(final_path), duration)
        finally:
            shutil.rmtree(output_dir, ignore_errors=True)

    def _run_compat_process(
        self,
        command: list[str],
        *,
        env: dict[str, str],
        should_cancel: Callable[[], bool],
        timeout_seconds: int,
        description: str,
        activity_probe: Callable[[], int] | None = None,
    ) -> tuple[int, str, str]:
        """Run yt-dlp while polling cancellation and enforcing a segment deadline."""
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            **_hidden_process_kwargs(),
        )
        if should_cancel():
            self._youtube._terminate_process_tree(process)  # noqa: SLF001
            process.communicate()
            raise YouTubeDownloadCancelled("Audio download cancelled.")
        output: dict[str, tuple[str, str]] = {}
        completed = Event()

        def collect_output() -> None:
            output["value"] = process.communicate()
            completed.set()

        # Even with yt-dlp progress hidden, delegated ffmpeg diagnostics can
        # exceed the OS pipe buffer. Drain continuously so a child cannot be
        # mistaken for a network timeout merely because its stderr is blocked.
        collector_thread = Thread(target=collect_output, name="youtube-compat-output", daemon=True)
        collector_thread.start()
        started_at = time.monotonic()
        deadline = time.monotonic() + max(1, int(timeout_seconds or 1))
        observed_bytes = 0
        first_output_at: float | None = None
        last_growth_at: float | None = None
        while not completed.wait(0.2):
            if activity_probe is not None:
                try:
                    current_bytes = max(0, int(activity_probe() or 0))
                except OSError:
                    current_bytes = observed_bytes
                if current_bytes > observed_bytes:
                    now = time.monotonic()
                    observed_bytes = current_bytes
                    if first_output_at is None:
                        first_output_at = now
                        LOGGER.info(
                            "Compatible YouTube audio temporary output started. task=%s first_byte_elapsed=%.3fs bytes=%s",
                            description,
                            max(now - started_at, 0.001),
                            observed_bytes,
                        )
                    last_growth_at = now
            if completed.is_set():
                break
            if should_cancel():
                self._youtube._terminate_process_tree(process)  # noqa: SLF001
                completed.wait(5)
                LOGGER.info("Compatible YouTube audio process cancelled. task=%s", description)
                raise YouTubeDownloadCancelled("Audio download cancelled.")
            now = time.monotonic()
            if first_output_at is None and now - started_at >= self._COMPAT_FIRST_OUTPUT_TIMEOUT_SECONDS:
                self._youtube._terminate_process_tree(process)  # noqa: SLF001
                completed.wait(5)
                stdout, stderr = output.get("value", ("", ""))
                diagnostics = self._compat_timeout_diagnostics(
                    started_at=started_at,
                    observed_bytes=observed_bytes,
                    first_output_at=first_output_at,
                    last_growth_at=last_growth_at,
                )
                LOGGER.warning(
                    "Compatible YouTube audio process produced no temporary output. task=%s first_output_timeout_seconds=%s diagnostics=%s output_tail=%s",
                    description,
                    self._COMPAT_FIRST_OUTPUT_TIMEOUT_SECONDS,
                    diagnostics,
                    self._compact_process_diagnostics(stderr or stdout),
                )
                raise YouTubeServiceError(
                    f"{description} did not produce temporary output within "
                    f"{self._COMPAT_FIRST_OUTPUT_TIMEOUT_SECONDS} seconds ({diagnostics})."
                )
            if now >= deadline:
                self._youtube._terminate_process_tree(process)  # noqa: SLF001
                completed.wait(5)
                stdout, stderr = output.get("value", ("", ""))
                diagnostics = self._compat_timeout_diagnostics(
                    started_at=started_at,
                    observed_bytes=observed_bytes,
                    first_output_at=first_output_at,
                    last_growth_at=last_growth_at,
                )
                LOGGER.warning(
                    "Compatible YouTube audio process timed out. task=%s timeout_seconds=%s diagnostics=%s output_tail=%s",
                    description,
                    timeout_seconds,
                    diagnostics,
                    self._compact_process_diagnostics(stderr or stdout),
                )
                raise YouTubeServiceError(
                    f"{description} timed out after {timeout_seconds} seconds ({diagnostics})."
                )
        collector_thread.join(timeout=0.1)
        stdout, stderr = output.get("value", ("", ""))
        return process.returncode or 0, stdout or "", stderr or ""

    @staticmethod
    def _compat_segment_bytes(output_dir: Path, index: int) -> int:
        """Report final and partial bytes written by one compatibility segment."""
        pattern = f"segment_{index:03d}.*"
        total = 0
        for path in output_dir.glob(pattern):
            if path.is_file():
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
        return total

    @staticmethod
    def _compat_timeout_diagnostics(
        *,
        started_at: float,
        observed_bytes: int,
        first_output_at: float | None,
        last_growth_at: float | None,
    ) -> str:
        """Classify a timeout without logging signed media URLs or cookie data."""
        now = time.monotonic()
        elapsed = max(now - started_at, 0.0)
        if first_output_at is None:
            return f"state=no_first_byte elapsed={elapsed:.1f}s bytes=0"
        first_byte_elapsed = max(first_output_at - started_at, 0.0)
        stalled_seconds = max(now - (last_growth_at or first_output_at), 0.0)
        state = "transfer_stalled" if stalled_seconds >= 5 else "finalizing_or_slow_transfer"
        return (
            f"state={state} elapsed={elapsed:.1f}s bytes={observed_bytes} "
            f"first_byte={first_byte_elapsed:.1f}s no_growth={stalled_seconds:.1f}s"
        )

    @staticmethod
    def _compact_process_diagnostics(value: str, *, limit: int = 700) -> str:
        """Keep useful process diagnostics while excluding signed media URLs."""
        normalized = re.sub(r"https?://[^\s'\"]+", "<url>", value or "")
        normalized = " ".join(normalized.split())
        if len(normalized) > limit:
            return normalized[-limit:]
        return normalized

    def _get_video_duration(self, source_url: str, *, should_cancel: Callable[[], bool] | None) -> int:
        options: dict[str, object] = {
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "extractor_args": {"youtube": {"player_client": ["android_vr"]}},
        }
        self._youtube._apply_synced_cookies(options)
        payload = self._youtube._extract_info_with_retry(
            options,
            source_url,
            download=False,
            should_cancel=should_cancel,
        )
        try:
            return max(0, int((payload or {}).get("duration") or 0))
        except (TypeError, ValueError):
            return 0
