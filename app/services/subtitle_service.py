from __future__ import annotations

import re
import time
from pathlib import Path

from app.models import CaptionCue, CaptionDocument, YouTubeVideo
from app.services.youtube_collector import YouTubeCollectionError, YouTubeCollector
from app.services.youtube_transcript_service import YouTubeTranscriptPanelService, YouTubeTranscriptUnavailable
from app.settings import CAPTION_DIR, ensure_runtime_directories


class SubtitleAcquisitionError(RuntimeError):
    pass


class YouTubeSubtitleService:
    """Fetch public captions first; callers can schedule ASR only when needed."""

    _LANGUAGE_PRIORITY = ("zh-Hans", "zh-Hant", "zh", "en", "ja", "ko")

    def __init__(
        self,
        collector: YouTubeCollector | None = None,
        transcript_panel_service: YouTubeTranscriptPanelService | None = None,
    ) -> None:
        self._collector = collector or YouTubeCollector()
        self._transcript_panel_service = transcript_panel_service or YouTubeTranscriptPanelService()

    def acquire_leading_captions(
        self,
        video: YouTubeVideo,
        *,
        leading_seconds: int = 180,
    ) -> CaptionDocument:
        ensure_runtime_directories()
        limit = max(1, int(leading_seconds or 180))
        panel_caption = self._acquire_transcript_panel(video, limit)
        if panel_caption is not None:
            return panel_caption
        try:
            payload = self._collector._extract(  # noqa: SLF001 - same focused YouTube acquisition boundary
                {
                    "skip_download": True,
                    "ignore_no_formats_error": True,
                    "quiet": True,
                    "no_warnings": True,
                    "extractor_args": {"youtube": {"player_client": ["android_vr"]}},
                },
                video.source_url,
                download=False,
            )
        except YouTubeCollectionError:
            # Some videos expose neither a transcript panel nor a downloadable
            # caption track to this yt-dlp client.  This is a per-video outcome,
            # never a reason to abort the whole batch.
            return self._asr_required_document(video, limit)
        manual_tracks = payload.get("subtitles") or {}
        automatic_tracks = payload.get("automatic_captions") or {}
        language_code, source_kind = self._choose_track(manual_tracks, automatic_tracks)
        if not language_code:
            return self._asr_required_document(video, limit)

        target_dir = CAPTION_DIR / video.video_id
        target_dir.mkdir(parents=True, exist_ok=True)
        before = {path.resolve() for path in target_dir.glob("*") if path.is_file()}
        options: dict[str, object] = {
            "skip_download": True,
            "writesubtitles": source_kind == "manual",
            "writeautomaticsub": source_kind == "automatic",
            "subtitleslangs": [language_code],
            "subtitlesformat": "srt/best",
            "outtmpl": str(target_dir / "%(id)s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "extractor_args": {"youtube": {"player_client": ["android_vr"]}},
        }
        self._collector._extract(options, video.source_url, download=True)  # noqa: SLF001
        candidates = [path for path in target_dir.glob("*") if path.is_file() and path.resolve() not in before]
        candidates = [path for path in candidates if path.suffix.lower() in {".srt", ".vtt"}]
        if not candidates:
            raise SubtitleAcquisitionError(f"Caption track {language_code} was announced but no file was downloaded.")

        source_path = max(candidates, key=lambda path: path.stat().st_mtime)
        cues = self._parse_caption_file(source_path)
        selected_cues = tuple(
            CaptionCue(start_seconds=start, end_seconds=end, text=cue_text)
            for start, end, cue_text in cues
            if start < limit
        )
        text = "\n".join(cue.text for cue in selected_cues).strip()
        if not text:
            return CaptionDocument(
                video=video,
                language_code=language_code,
                source_kind=source_kind,
                source_path=str(source_path),
                text="",
                start_seconds=0,
                end_seconds=limit,
                asr_required=True,
                cues=selected_cues,
            )
        return CaptionDocument(
            video=video,
            language_code=language_code,
            source_kind=source_kind,
            source_path=str(source_path),
            text=text,
            start_seconds=0,
            end_seconds=limit,
            cues=selected_cues,
        )

    @staticmethod
    def _asr_required_document(video: YouTubeVideo, limit: int) -> CaptionDocument:
        return CaptionDocument(
            video=video,
            language_code="",
            source_kind="none",
            source_path="",
            text="",
            start_seconds=0,
            end_seconds=limit,
            asr_required=True,
        )

    def _acquire_transcript_panel(self, video: YouTubeVideo, limit: int) -> CaptionDocument | None:
        result = None
        for attempt in range(2):
            try:
                result = self._transcript_panel_service.acquire_leading_transcript(
                    video,
                    leading_seconds=limit,
                )
                break
            except YouTubeTranscriptUnavailable:
                if attempt == 0:
                    time.sleep(0.8)
        if result is None:
            return None
        target_dir = CAPTION_DIR / video.video_id
        target_dir.mkdir(parents=True, exist_ok=True)
        source_path = target_dir / f"{video.video_id}.transcript.txt"
        source_path.write_text(result.text, encoding="utf-8")
        return CaptionDocument(
            video=video,
            language_code=result.language_code,
            source_kind="youtube_transcript_panel",
            source_path=str(source_path),
            text=result.text,
            start_seconds=result.start_seconds,
            end_seconds=result.end_seconds,
            cues=result.cues,
        )

    @classmethod
    def _choose_track(cls, manual_tracks: dict, automatic_tracks: dict) -> tuple[str, str]:
        for source_kind, tracks in (("manual", manual_tracks), ("automatic", automatic_tracks)):
            if not isinstance(tracks, dict):
                continue
            for language in cls._LANGUAGE_PRIORITY:
                if tracks.get(language):
                    return language, source_kind
            for language, formats in tracks.items():
                if formats:
                    return str(language), source_kind
        return "", ""

    @staticmethod
    def _parse_caption_file(path: Path) -> list[tuple[float, float, str]]:
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix.lower() == ".srt":
            return YouTubeCollector.srt_cues(text)

        cues: list[tuple[float, float, str]] = []
        for block in re.split(r"\r?\n\s*\r?\n", text):
            lines = [line.strip() for line in block.splitlines() if line.strip() and not line.startswith("WEBVTT")]
            timing_index = next((index for index, line in enumerate(lines) if "-->" in line), -1)
            if timing_index < 0:
                continue
            try:
                start_raw, end_raw = (part.strip() for part in lines[timing_index].split("-->", 1))
                start = YouTubeCollector._srt_time_to_seconds(start_raw)
                end = YouTubeCollector._srt_time_to_seconds(end_raw.split()[0])
            except (TypeError, ValueError):
                continue
            cue_text = re.sub(r"<[^>]+>", "", " ".join(lines[timing_index + 1 :]))
            if cue_text:
                cues.append((start, end, cue_text))
        return cues
