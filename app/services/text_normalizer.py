from __future__ import annotations

import re


class SubtitleTextNormalizer:
    def normalize(self, text: str, *, language_code: str = "") -> str:
        value = str(text or "")
        value = re.sub(r"\[[^\]]{1,80}\]", " ", value)
        value = re.sub(r"\s+", " ", value)
        return value.strip()

    @staticmethod
    def matching_language_code(source_language_code: str) -> str:
        """Map YouTube track labels to the matching service's language partitions only."""
        value = str(source_language_code or "").lower().replace("_", "-")
        if value.startswith("zh"):
            return "zh"
        if value.startswith("en"):
            return "en"
        if value.startswith("ja"):
            return "ja"
        if value.startswith("ko"):
            return "ko"
        return ""
