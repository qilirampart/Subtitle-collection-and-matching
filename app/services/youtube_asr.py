from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app.services.api_config_service import ApiConfigService
from app.services.audio_transcription_service import AudioTranscriptionService
from app.services.youtube_audio_service import YouTubeAudioService


class YouTubeAsrError(RuntimeError):
    pass


@dataclass(frozen=True)
class AsrTranscript:
    source_path: str
    audio_path: str
    text: str


class YouTubeAsrService:
    """Adapter over the original resilient ASR stack migrated from the material helper."""

    def __init__(self) -> None:
        self._config_service = ApiConfigService()
        self._audio_service = YouTubeAudioService()
        self._transcription_service = AudioTranscriptionService()

    def is_ready(self) -> bool:
        config = self._config_service.load_config()
        if not bool((config.get("asr") or {}).get("enabled", True)):
            return False
        return bool(self._config_service.list_asr_providers(require_secret=True))

    def download_video_audio(
        self,
        source_url: str,
        *,
        leading_seconds: int,
        segment_concurrency: int = 6,
        should_cancel: Callable[[], bool] | None = None,
    ) -> str:
        if not self.is_ready():
            raise YouTubeAsrError("ASR is not configured. Configure and enable at least one ASR provider first.")
        audio = self._audio_service.download_audio(
            source_url,
            max_duration_seconds=max(1, int(leading_seconds or 180)),
            concurrency=max(1, min(int(segment_concurrency or 1), 6)),
            should_cancel=should_cancel,
        )
        return audio.local_path

    def transcribe_video(
        self,
        source_url: str,
        *,
        leading_seconds: int,
        segment_concurrency: int = 6,
        should_cancel: Callable[[], bool] | None = None,
    ) -> AsrTranscript:
        audio_path = self.download_video_audio(
            source_url,
            leading_seconds=leading_seconds,
            segment_concurrency=segment_concurrency,
            should_cancel=should_cancel,
        )
        return self.transcribe_audio_source(audio_path, should_cancel=should_cancel)

    def transcribe_audio_source(
        self,
        source_path: str,
        *,
        should_cancel: Callable[[], bool] | None = None,
    ) -> AsrTranscript:
        if not self.is_ready():
            raise YouTubeAsrError("ASR is not configured. Configure and enable at least one ASR provider first.")
        prepared, transcription = self._transcription_service.transcribe_source(
            source_path,
            should_cancel=should_cancel,
        )
        text = transcription.text.strip()
        if not text:
            raise YouTubeAsrError("ASR returned an empty transcript.")
        return AsrTranscript(source_path=source_path, audio_path=prepared.audio_path, text=text)
