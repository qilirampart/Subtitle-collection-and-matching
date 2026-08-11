from __future__ import annotations

import argparse
import csv
import json
import multiprocessing
import time
from datetime import datetime
from pathlib import Path
from queue import Empty

from app.config.settings import LOG_DIR, OUTPUT_DIR
from app.models import YouTubeVideo
from app.services.subtitle_service import YouTubeSubtitleService
from app.services.youtube_audio_service import YouTubeAudioService


def _caption_worker(video_data: dict[str, object], seconds: int, queue) -> None:
    video = YouTubeVideo(**video_data)
    started = time.perf_counter()
    try:
        caption = YouTubeSubtitleService().acquire_leading_captions(video, leading_seconds=seconds)
        queue.put({
            "status": "ready" if not caption.asr_required else "missing",
            "source": caption.source_kind,
            "chars": len(caption.text),
            "seconds": round(time.perf_counter() - started, 3),
            "error": "",
        })
    except Exception as exc:  # noqa: BLE001
        queue.put({
            "status": "error",
            "source": "",
            "chars": 0,
            "seconds": round(time.perf_counter() - started, 3),
            "error": type(exc).__name__,
        })


def _route_from_log(log_path: Path, offset: int) -> tuple[str, str]:
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as stream:
            stream.seek(offset)
            text = stream.read()
    except OSError:
        return "unknown", ""
    if "Direct YouTube audio Range request succeeded" in text:
        return "direct_range", ""
    for line in text.splitlines():
        if "Direct YouTube audio range download unavailable" in line:
            return "compat_fallback", line.rsplit("reason=", 1)[-1].strip()
    return "unknown", ""


def _load_videos(state_path: Path, limit: int) -> list[YouTubeVideo]:
    state = json.loads(state_path.read_text(encoding="utf-8"))
    videos = [YouTubeVideo(**raw) for raw in state.get("videos", []) if isinstance(raw, dict)]
    if not videos:
        videos = [
            YouTubeVideo(
                video_id=str(row.get("source_video_id") or ""),
                source_url=str(row.get("source_url") or ""),
                title=str(row.get("source_title") or row.get("source_video_id") or ""),
                channel=str(row.get("source_channel") or ""),
            )
            for row in state.get("matching_result_rows", [])
            if isinstance(row, dict) and row.get("source_video_id") and row.get("source_url")
        ]
    return videos[: max(1, limit)]


def main() -> int:
    parser = argparse.ArgumentParser(description="Caption then audio benchmark without ASR.")
    parser.add_argument("--session-state", default="runtime/session_state.json")
    parser.add_argument("--limit", type=int, default=15)
    parser.add_argument("--seconds", type=int, default=180)
    parser.add_argument("--caption-timeout", type=int, default=75)
    args = parser.parse_args()

    started_at = datetime.now()
    output_dir = OUTPUT_DIR / "benchmarks"
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = started_at.strftime("%Y%m%d_%H%M%S")
    videos = _load_videos(Path(args.session_state), args.limit)
    log_path = LOG_DIR / "app.log"
    audio_service = YouTubeAudioService()
    context = multiprocessing.get_context("spawn")
    records: list[dict[str, object]] = []

    for index, video in enumerate(videos, start=1):
        record: dict[str, object] = {
            "index": index,
            "video_id": video.video_id,
            "title": video.title,
            "url": video.source_url,
            "caption_status": "",
            "caption_source": "",
            "caption_chars": 0,
            "caption_seconds": 0.0,
            "audio_status": "not_needed",
            "audio_route": "",
            "audio_fallback_reason": "",
            "audio_seconds": 0.0,
            "audio_path": "",
            "error": "",
        }
        print(f"[{index}/{len(videos)}] caption probe: {video.video_id}", flush=True)
        result_queue = context.Queue()
        worker = context.Process(target=_caption_worker, args=(video.to_dict(), args.seconds, result_queue))
        worker.start()
        worker.join(max(1, args.caption_timeout))
        if worker.is_alive():
            worker.terminate()
            worker.join(5)
            record.update({"caption_status": "timeout", "caption_seconds": float(args.caption_timeout), "error": "caption_timeout"})
        else:
            try:
                caption_result = result_queue.get(timeout=3)
            except Empty:
                caption_result = {"status": "error", "source": "", "chars": 0, "seconds": 0.0, "error": "caption_worker_no_result"}
            record.update({
                "caption_status": caption_result["status"],
                "caption_source": caption_result["source"],
                "caption_chars": caption_result["chars"],
                "caption_seconds": caption_result["seconds"],
                "error": caption_result["error"],
            })
        result_queue.close()

        if record["caption_status"] == "ready":
            print(f"  direct caption ready in {record['caption_seconds']}s", flush=True)
            records.append(record)
            continue

        print(f"  direct caption {record['caption_status']}; downloading audio only", flush=True)
        log_offset = log_path.stat().st_size if log_path.is_file() else 0
        audio_started = time.perf_counter()
        try:
            audio_result = audio_service.download_audio(video.source_url, max_duration_seconds=args.seconds, concurrency=6)
            route, reason = _route_from_log(log_path, log_offset)
            record.update({
                "audio_status": "downloaded",
                "audio_route": route,
                "audio_fallback_reason": reason,
                "audio_seconds": round(time.perf_counter() - audio_started, 3),
                "audio_path": audio_result.local_path,
            })
            print(f"  audio {route} in {record['audio_seconds']}s", flush=True)
        except Exception as exc:  # noqa: BLE001
            record.update({
                "audio_status": "error",
                "audio_seconds": round(time.perf_counter() - audio_started, 3),
                "error": f"{record['error']} | audio:{type(exc).__name__}".strip(" |"),
            })
            print(f"  audio failed: {type(exc).__name__}", flush=True)
        records.append(record)

    csv_path = output_dir / f"caption_then_audio_isolated_{stamp}.csv"
    json_path = output_dir / f"caption_then_audio_isolated_{stamp}.json"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(records[0]) if records else ["index"])
        writer.writeheader()
        writer.writerows(records)
    json_path.write_text(json.dumps({"seconds": args.seconds, "caption_timeout": args.caption_timeout, "records": records}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"csv={csv_path.resolve()}")
    print(f"json={json_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
