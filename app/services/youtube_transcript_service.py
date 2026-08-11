from __future__ import annotations

import json
import re
from dataclasses import dataclass
from http.cookiejar import MozillaCookieJar
from typing import Iterator

import requests

from app.models import CaptionCue, YouTubeVideo
from app.settings import YOUTUBE_COOKIES_PATH, YOUTUBE_PROXY_CONFIG_PATH


class YouTubeTranscriptUnavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class YouTubeTranscriptPanelResult:
    text: str
    language_code: str
    start_seconds: int
    end_seconds: int
    segment_count: int
    cues: tuple[CaptionCue, ...]


class YouTubeTranscriptPanelService:
    """Reads the structured transcript panel rendered on YouTube watch pages."""

    _WATCH_HEADERS = {
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
    }
    _INITIAL_DATA_MARKERS = ("var ytInitialData =", "ytInitialData =")

    def acquire_leading_transcript(
        self,
        video: YouTubeVideo,
        *,
        leading_seconds: int = 180,
    ) -> YouTubeTranscriptPanelResult:
        session = self._build_session()
        try:
            page = session.get(video.source_url, headers=self._WATCH_HEADERS, timeout=30)
            page.raise_for_status()
            initial_data = self._extract_initial_data(page.text)
            panel_id, params = self._extract_panel_request(initial_data)
            response = session.post(
                "https://www.youtube.com/youtubei/v1/get_panel?prettyPrint=false",
                headers={
                    **self._WATCH_HEADERS,
                    "Origin": "https://www.youtube.com",
                    "Referer": "https://www.youtube.com/",
                },
                json={
                    "context": {"client": self._extract_client_context(page.text)},
                    "panelId": panel_id,
                    "params": params,
                },
                timeout=30,
            )
            response.raise_for_status()
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            raise YouTubeTranscriptUnavailable(str(exc)) from exc

        limit = max(1, int(leading_seconds or 180))
        segments = [
            item["transcriptSegmentViewModel"]
            for item in self._walk(response.json())
            if "transcriptSegmentViewModel" in item
        ]
        cues: list[CaptionCue] = []
        for index, segment in enumerate(segments):
            start = self._timestamp_to_seconds(str(segment.get("timestamp") or ""))
            if start is None or start >= limit:
                continue
            text = str(segment.get("simpleText") or "").strip()
            if not text:
                continue
            next_start = self._timestamp_to_seconds(
                str(segments[index + 1].get("timestamp") or "")
            ) if index + 1 < len(segments) else None
            end = min(limit, next_start) if next_start is not None else limit
            cues.append(CaptionCue(start_seconds=float(start), end_seconds=float(max(start, end)), text=text))
        text = "\n".join(cue.text for cue in cues).strip()
        if not text:
            raise YouTubeTranscriptUnavailable("YouTube transcript panel contains no usable text.")
        return YouTubeTranscriptPanelResult(
            text=text,
            language_code=self._infer_language_code(text),
            start_seconds=0,
            end_seconds=limit,
            segment_count=len(segments),
            cues=tuple(cues),
        )

    @classmethod
    def _build_session(cls) -> requests.Session:
        session = requests.Session()
        if YOUTUBE_COOKIES_PATH.is_file() and YOUTUBE_COOKIES_PATH.stat().st_size > 0:
            jar = MozillaCookieJar(str(YOUTUBE_COOKIES_PATH))
            try:
                jar.load(ignore_discard=True, ignore_expires=True)
            except OSError:
                pass
            else:
                session.cookies = jar
        try:
            proxy = YOUTUBE_PROXY_CONFIG_PATH.read_text(encoding="utf-8").strip()
        except OSError:
            proxy = ""
        if proxy.lower().startswith(("http://", "https://")):
            session.proxies.update({"http": proxy, "https": proxy})
        return session

    @classmethod
    def _extract_initial_data(cls, page: str) -> dict:
        decoder = json.JSONDecoder()
        for marker in cls._INITIAL_DATA_MARKERS:
            index = page.find(marker)
            if index < 0:
                continue
            payload, _end = decoder.raw_decode(page[index + len(marker) :].lstrip())
            if isinstance(payload, dict):
                return payload
        raise YouTubeTranscriptUnavailable("YouTube watch page has no transcript configuration.")

    @classmethod
    def _extract_panel_request(cls, initial_data: dict) -> tuple[str, str]:
        # The legacy watch-page button opens the transcript with this endpoint.
        for item in cls._walk(initial_data):
            section = item.get("videoDescriptionTranscriptSectionRenderer")
            if not isinstance(section, dict):
                continue
            commands = (
                section.get("primaryButton", {})
                .get("buttonRenderer", {})
                .get("command", {})
                .get("commandExecutorCommand", {})
                .get("commands", [])
            )
            for command in commands:
                endpoint = command.get("showEngagementPanelEndpoint", {}) if isinstance(command, dict) else {}
                panel_id = str(endpoint.get("identifier", {}).get("tag") or "").strip()
                params = str(endpoint.get("globalConfiguration", {}).get("params") or "").strip()
                if panel_id and params:
                    return panel_id, params

        # Newer watch pages use an in-place engagement-panel update instead of
        # showEngagementPanelEndpoint.  The panel and request params are still
        # supplied by the page, just under a different command payload.
        for item in cls._walk(initial_data):
            command = item.get("updateEngagementPanelContentCommand")
            if not isinstance(command, dict):
                continue
            panel_id = str(
                command.get("contentSourcePanelIdentifier", {}).get("tag") or ""
            ).strip()
            params = str(command.get("globalConfiguration", {}).get("params") or "").strip()
            if panel_id and "transcript" in panel_id.lower() and params:
                return panel_id, params
        raise YouTubeTranscriptUnavailable("This video does not expose a transcript panel.")

    @classmethod
    def _extract_client_context(cls, page: str) -> dict[str, str]:
        version_match = re.search(r'"INNERTUBE_CONTEXT_CLIENT_VERSION":"([^"]+)"', page)
        if not version_match:
            raise YouTubeTranscriptUnavailable("YouTube client version is unavailable.")
        client = {"clientName": "WEB", "clientVersion": version_match.group(1), "hl": "zh-CN", "gl": "US"}
        visitor_match = re.search(r'"VISITOR_DATA":"([^"]+)"', page)
        if visitor_match:
            client["visitorData"] = visitor_match.group(1)
        return client

    @staticmethod
    def _walk(value: object) -> Iterator[dict]:
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from YouTubeTranscriptPanelService._walk(child)
        elif isinstance(value, list):
            for child in value:
                yield from YouTubeTranscriptPanelService._walk(child)

    @staticmethod
    def _timestamp_to_seconds(value: str) -> int | None:
        parts = value.strip().split(":")
        if not parts or len(parts) > 3:
            return None
        try:
            values = [int(part) for part in parts]
        except ValueError:
            return None
        if any(part < 0 for part in values):
            return None
        total = 0
        for part in values:
            total = total * 60 + part
        return total

    @staticmethod
    def _infer_language_code(text: str) -> str:
        value = str(text or "")
        if re.search(r"[\uac00-\ud7af]", value):
            return "ko"
        if re.search(r"[\u3040-\u30ff]", value):
            return "ja"
        if re.search(r"[\u3400-\u9fff]", value):
            return "zh"
        if re.search(r"[A-Za-z]", value):
            return "en"
        return ""


__all__ = ["YouTubeTranscriptPanelResult", "YouTubeTranscriptPanelService", "YouTubeTranscriptUnavailable"]
