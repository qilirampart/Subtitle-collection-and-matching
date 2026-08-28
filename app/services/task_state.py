from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from app.settings import TASK_STATE_PATH


def has_recoverable_work(payload: dict[str, Any]) -> bool:
    """Return whether a saved workspace contains work worth offering again.

    ``active`` is only a best-effort runtime flag. It can be written as false
    when a worker reports an error just before the process exits, so recovery
    also checks the durable queues and the number of processed video IDs.
    """
    if bool(payload.get("active")):
        return True

    status = str(payload.get("status") or "")
    if status.startswith(("正在", "恢复")) or "未完成" in status or "失败" in status:
        return True

    pending_asr = payload.get("pending_asr")
    pending_items = pending_asr if isinstance(pending_asr, list) else []
    if pending_items:
        return True

    task_spec = payload.get("task_spec")
    videos = payload.get("videos")
    if not isinstance(task_spec, dict) or not isinstance(videos, list) or not videos:
        return False

    kind = str(task_spec.get("kind") or "")
    if kind not in {"prepare", "asr_fallback"}:
        return False

    ready_ids = {
        str(item.get("source_video_id") or "")
        for item in payload.get("ready_items", [])
        if isinstance(item, dict) and str(item.get("source_video_id") or "")
    }
    pending_ids = {
        str((item.get("video") or {}).get("video_id") or "")
        for item in pending_items
        if isinstance(item, dict) and isinstance(item.get("video"), dict)
    }
    video_ids = {
        str(item.get("video_id") or "")
        for item in videos
        if isinstance(item, dict) and str(item.get("video_id") or "")
    }
    return bool(video_ids - ready_ids - pending_ids)


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
