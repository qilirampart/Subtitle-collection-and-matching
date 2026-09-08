"""Conservative concurrency policy for simultaneous local workflows."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ResourceLimits:
    cover_download: int
    cover_review: int
    subtitle_caption: int
    audio_download: int
    asr: int


class ResourceGovernor:
    """Calculate bounded lane concurrency without creating extra queues."""

    _STABLE = ResourceLimits(cover_download=2, cover_review=1, subtitle_caption=2, audio_download=1, asr=1)
    _BALANCED = ResourceLimits(cover_download=3, cover_review=2, subtitle_caption=2, audio_download=2, asr=2)

    def __init__(self, mode: str = "stable") -> None:
        normalized = str(mode or "stable").strip().lower()
        if normalized not in {"stable", "balanced"}:
            normalized = "stable"
        self.mode = normalized

    def limits(self, *, asr_active: bool = False) -> ResourceLimits:
        base = self._BALANCED if self.mode == "balanced" else self._STABLE
        if not asr_active:
            return base
        return ResourceLimits(
            cover_download=min(base.cover_download, 1),
            cover_review=min(base.cover_review, 1),
            subtitle_caption=base.subtitle_caption,
            audio_download=base.audio_download,
            asr=base.asr,
        )

    def clamp(self, resource: str, requested: int, *, asr_active: bool = False) -> int:
        limits = self.limits(asr_active=asr_active)
        limit = int(getattr(limits, resource, 1))
        return max(1, min(int(requested or 1), limit))
