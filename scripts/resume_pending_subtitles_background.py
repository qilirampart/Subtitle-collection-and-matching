from __future__ import annotations

import json
import sys
import argparse
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.models import YouTubeVideo
from app.services.subtitle_excel_exporter import export_subtitles_to_xlsx
from app.services.youtube_collector import YouTubeCollector
from app.workflow import VerificationWorkflow


MANIFEST_PATH = PROJECT_ROOT / "runtime" / "recovery" / "asr_resume_manifest_20260808_123028.json"
STATE_PATH = PROJECT_ROOT / "runtime" / "recovery" / "background_subtitle_resume_state.json"
OUTPUT_PATH = PROJECT_ROOT / "output" / "youtube_subtitles_resumed_20260809.xlsx"
LOG_PATH = PROJECT_ROOT / "output" / "logs" / "background_subtitle_resume.log"
BATCH_SIZE = 5


def log(message: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}"
    print(line, flush=True)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")


def load_json(path: Path, fallback: dict[str, object]) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(fallback)
    return payload if isinstance(payload, dict) else dict(fallback)


def save_state(state: dict[str, object]) -> None:
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(STATE_PATH)


def main() -> int:
    parser = argparse.ArgumentParser(description="Resume recovered YouTube subtitles in the background.")
    parser.add_argument("--download-concurrency", type=int, default=2)
    parser.add_argument("--asr-concurrency", type=int, default=2)
    args = parser.parse_args()
    download_concurrency = max(1, min(int(args.download_concurrency or 1), 3))
    asr_concurrency = max(1, min(int(args.asr_concurrency or 1), 3))
    manifest = load_json(MANIFEST_PATH, {})
    pending_ids = [str(value).strip() for value in manifest.get("pending_ids", []) if str(value).strip()]
    channel = str(manifest.get("channel") or "").strip()
    if len(pending_ids) != 75 or not channel:
        raise RuntimeError("The recovered 75-video manifest is unavailable or invalid.")

    state = load_json(STATE_PATH, {"completed_ids": [], "ready_items": [], "failed": {}})
    completed_ids = {str(value).strip() for value in state.get("completed_ids", []) if str(value).strip()}
    ready_items = [dict(item) for item in state.get("ready_items", []) if isinstance(item, dict)]
    failed = dict(state.get("failed", {})) if isinstance(state.get("failed"), dict) else {}

    log("Collecting the source channel to rebuild pending video metadata.")
    videos = YouTubeCollector().collect_channel(channel, max_items=0)
    by_id = {video.video_id: video for video in videos}
    missing = [video_id for video_id in pending_ids if video_id not in by_id]
    if missing:
        raise RuntimeError(f"Source channel no longer contains {len(missing)} pending videos; refusing unsafe resume.")
    remaining = [by_id[video_id] for video_id in pending_ids if video_id not in completed_ids]
    log(f"Resume status: completed={len(completed_ids)} remaining={len(remaining)} failed={len(failed)}.")

    workflow = VerificationWorkflow()
    for offset in range(0, len(remaining), BATCH_SIZE):
        batch = remaining[offset : offset + BATCH_SIZE]
        batch_number = offset // BATCH_SIZE + 1
        log(
            f"Batch {batch_number}: processing {len(batch)} videos "
            f"(download={download_concurrency}, asr={asr_concurrency})."
        )
        ready, still_pending = workflow.prepare_batch_items(
            batch,
            leading_seconds=180,
            allow_asr_fallback=True,
            caption_concurrency=2,
            download_concurrency=download_concurrency,
            asr_concurrency=asr_concurrency,
            stage_callback=log,
        )
        ready_items.extend(ready)
        ready_ids = {str(item.get("source_video_id") or "") for item in ready}
        completed_ids.update(video_id for video_id in ready_ids if video_id)
        for item in still_pending:
            video = item.get("video") if isinstance(item, dict) else {}
            inspection = item.get("inspection") if isinstance(item, dict) else {}
            video_id = str(video.get("video_id") or "") if isinstance(video, dict) else ""
            if video_id:
                failed[video_id] = str(inspection.get("asr_error") or inspection.get("asr_status") or "pending")
        for video_id in ready_ids:
            failed.pop(video_id, None)

        unique_items: dict[str, dict[str, object]] = {}
        for item in ready_items:
            video_id = str(item.get("source_video_id") or "")
            if video_id:
                unique_items[video_id] = item
        ready_items = list(unique_items.values())
        state = {
            "manifest_path": str(MANIFEST_PATH),
            "completed_ids": sorted(completed_ids),
            "ready_items": ready_items,
            "failed": failed,
            "output_path": str(OUTPUT_PATH),
        }
        save_state(state)
        export_subtitles_to_xlsx(OUTPUT_PATH, ready_items, completed_ids)
        log(f"Batch {batch_number} checkpoint: new_ready={len(ready_ids)} total_ready={len(completed_ids)} failed={len(failed)}.")

    log(f"Background resume completed: ready={len(completed_ids)} failed={len(failed)} output={OUTPUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
