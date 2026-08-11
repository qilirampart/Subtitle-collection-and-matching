from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from app.settings import TASK_STATE_PATH


class TaskStateStore:
    def load(self) -> dict[str, Any] | None:
        if not TASK_STATE_PATH.is_file():
            return None
        try:
            payload = json.loads(TASK_STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def save(self, payload: dict[str, Any]) -> None:
        TASK_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        snapshot = dict(payload)
        snapshot["updated_at"] = datetime.now().isoformat(timespec="seconds")
        temporary_path = TASK_STATE_PATH.with_suffix(".tmp")
        temporary_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary_path.replace(TASK_STATE_PATH)

