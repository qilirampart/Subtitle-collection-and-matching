from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from app.settings import TASK_STATE_PATH

TASK_STATE_VERSION = 2
TASK_LANES = ("cover_lane", "subtitle_lane", "matching_lane")


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _empty_lane() -> dict[str, Any]:
    return {
        "active": False,
        "stage": "",
        "status": "",
        "task_spec": {},
        "progress": {},
    }


def _legacy_lanes(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Infer v2 lane metadata from the previous single-task snapshot."""
    active = bool(payload.get("active"))
    task_kind = str(payload.get("task_kind") or "")
    status = str(payload.get("status") or "")
    task_spec = _mapping(payload.get("task_spec"))
    kind = str(task_spec.get("kind") or "")

    cover = _empty_lane()
    if active and "封面" in task_kind:
        cover.update(
            active=True,
            stage="review" if "检测" in task_kind else "download" if "下载" in task_kind else "collect",
            status=status,
            task_spec=task_spec,
        )

    subtitle = _empty_lane()
    if active and ("字幕" in task_kind or "ASR" in task_kind):
        subtitle.update(
            active=True,
            stage=kind if kind in {"prepare", "asr_fallback"} else "subtitle",
            status=status,
            task_spec=task_spec,
        )

    matching = _empty_lane()
    if active and "匹配" in task_kind:
        matching.update(active=True, stage="matching", status=status, task_spec=task_spec)

    return {
        "cover_lane": cover,
        "subtitle_lane": subtitle,
        "matching_lane": matching,
    }


def normalize_task_state(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a v2 task snapshot while retaining all legacy restore fields.

    The UI still restores the legacy top-level fields during the migration.
    ``lanes`` is additive so later versions can schedule cover, subtitle and
    matching work independently without invalidating snapshots created before
    the split.
    """
    snapshot = dict(payload)
    existing_lanes = _mapping(snapshot.get("lanes"))
    inferred_lanes = _legacy_lanes(snapshot)
    normalized_lanes: dict[str, dict[str, Any]] = {}
    for lane_name in TASK_LANES:
        lane = _empty_lane()
        lane.update(inferred_lanes[lane_name])
        lane.update(_mapping(existing_lanes.get(lane_name)))
        lane["active"] = bool(lane.get("active"))
        lane["stage"] = str(lane.get("stage") or "")
        lane["status"] = str(lane.get("status") or "")
        lane["task_spec"] = _mapping(lane.get("task_spec"))
        lane["progress"] = _mapping(lane.get("progress"))
        normalized_lanes[lane_name] = lane

    snapshot["version"] = TASK_STATE_VERSION
    snapshot["lanes"] = normalized_lanes
    return snapshot


def has_recoverable_work(payload: dict[str, Any]) -> bool:
    """Return whether a saved workspace contains work worth offering again.

    ``active`` is only a best-effort runtime flag. It can be written as false
    when a worker reports an error just before the process exits, so recovery
    also checks the durable queues and the number of processed video IDs.
    """
    payload = normalize_task_state(payload)
    lanes = _mapping(payload.get("lanes"))
    if any(bool(_mapping(lanes.get(name)).get("active")) for name in TASK_LANES):
        return True

    if bool(payload.get("active")):
        return True

    status = str(payload.get("status") or "")
    if status.startswith(("正在", "恢复")) or "未完成" in status or "失败" in status:
        return True

    pending_asr = payload.get("pending_asr")
    pending_items = pending_asr if isinstance(pending_asr, list) else []
    if pending_items:
        return True

    cover_queue = payload.get("cover_queue_items")
    if isinstance(cover_queue, list) and cover_queue:
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
        return normalize_task_state(payload) if isinstance(payload, dict) else None

    def save(self, payload: dict[str, Any]) -> None:
        TASK_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        snapshot = normalize_task_state(payload)
        snapshot["updated_at"] = datetime.now().isoformat(timespec="seconds")
        temporary_path = TASK_STATE_PATH.with_suffix(".tmp")
        temporary_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary_path.replace(TASK_STATE_PATH)
