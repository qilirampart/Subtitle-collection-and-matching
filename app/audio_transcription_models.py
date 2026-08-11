from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TranscriptWord:
    text: str
    start_ms: int
    end_ms: int


@dataclass
class TranscriptSegment:
    text: str
    start_ms: int
    end_ms: int
    speaker_id: int = 0
    words: list[TranscriptWord] = field(default_factory=list)


@dataclass
class PreparedAudio:
    source_path: str
    audio_path: str
    duration_ms: int
    size_bytes: int
    chunk_paths: list[str] = field(default_factory=list)
    chunk_offsets_ms: list[int] = field(default_factory=list)


@dataclass
class AudioTranscriptionResult:
    source_path: str
    audio_path: str
    text: str
    srt_text: str
    segments: list[TranscriptSegment] = field(default_factory=list)
    raw_tasks: list[dict] = field(default_factory=list)
    raw_text: str = ""
    corrected_text: str = ""
    correction_applied: bool = False
    correction_notes: str = ""

    def __post_init__(self) -> None:
        if not self.raw_text:
            self.raw_text = self.text
        if not self.corrected_text:
            self.corrected_text = self.text
