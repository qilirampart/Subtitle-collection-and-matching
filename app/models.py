from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class YouTubeVideo:
    video_id: str
    source_url: str
    title: str
    channel: str = ""
    upload_date: str = ""
    duration_seconds: int = 0
    thumbnail_url: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class CaptionCue:
    start_seconds: float
    end_seconds: float
    text: str


@dataclass(frozen=True)
class CaptionDocument:
    video: YouTubeVideo
    language_code: str
    source_kind: str
    source_path: str
    text: str
    start_seconds: int
    end_seconds: int
    asr_required: bool = False
    cues: tuple[CaptionCue, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return asdict(self)
