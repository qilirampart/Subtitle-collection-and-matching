from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import datetime
from pathlib import Path

# Allow direct execution with `python scripts/benchmark_caption_then_audio.py`.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config.settings import LOG_DIR, OUTPUT_DIR
from app.models import YouTubeVideo
from app.services.subtitle_service import YouTubeSubtitleService
from app.services.youtube_audio_service import YouTubeAudioService
from app.services.youtube_collector import YouTubeCollector


DEFAULT_CHANNEL_URL = "https://www.youtube.com/@%E9%9B%B2%E4%B8%8A%E5%8A%87%E5%A0%B4_99"


def _route_from_log(log_path: Path, offset: int) -> tuple[str, str]:
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as stream:
            stream.seek(offset)
            text = stream.read()
    except OSError:
        return "unknown", ""
    if (
        "Direct YouTube audio Range request succeeded" in text
        or "Direct YouTube audio Range retry succeeded" in text
    ):
        return "direct_range", ""
    if "Direct YouTube audio range download unavailable" in text:
        reason = ""
        for line in text.splitlines():
            if "Direct YouTube audio range download unavailable" in line:
                reason = line.rsplit("reason=", 1)[-1].strip() if "reason=" in line else "fallback"
                break
        return "compat_fallback", reason
    return "unknown", ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark YouTube captions then leading-audio download without ASR.")
    parser.add_argument("--channel", default=DEFAULT_CHANNEL_URL)
    parser.add_argument("--limit", type=int, default=15)
    parser.add_argument("--seconds", type=int, default=180)
    parser.add_argument(
        "--audio-concurrency",
        type=int,
        default=6,
        help="Compatibility segmented-download worker count when fallback is used.",
    )
    parser.add_argument(
        "--force-compat",
        action="store_true",
        help="Skip the direct Range route to benchmark the compatibility fallback only.",
    )
    parser.add_argument(
        "--compat-segment-seconds",
        type=int,
        default=30,
        help="Compatibility segment duration for benchmark comparison only.",
    )
    parser.add_argument("--session-state", default="", help="Use already collected videos from a session-state JSON file.")
    args = parser.parse_args()

    started_at = datetime.now()
    output_dir = OUTPUT_DIR / "benchmarks"
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = started_at.strftime("%Y%m%d_%H%M%S")
    csv_path = output_dir / f"caption_then_audio_{stamp}.csv"
    json_path = output_dir / f"caption_then_audio_{stamp}.json"
    log_path = LOG_DIR / "app.log"

    collector = YouTubeCollector()
    subtitles = YouTubeSubtitleService(collector)
    audio = YouTubeAudioService()
    if args.compat_segment_seconds > 0:
        audio._COMPAT_SEGMENT_SECONDS = max(15, args.compat_segment_seconds)  # noqa: SLF001
    collect_started = time.perf_counter()
    if args.session_state:
        raw_state = json.loads(Path(args.session_state).read_text(encoding="utf-8"))
        videos = [
            YouTubeVideo(**raw)
            for raw in raw_state.get("videos", [])
            if isinstance(raw, dict)
        ]
        if not videos:
            videos = [
                YouTubeVideo(
                    video_id=str(row.get("source_video_id") or ""),
                    source_url=str(row.get("source_url") or ""),
                    title=str(row.get("source_title") or row.get("source_video_id") or ""),
                    channel=str(row.get("source_channel") or ""),
                )
                for row in raw_state.get("matching_result_rows", [])
                if isinstance(row, dict)
                and str(row.get("source_video_id") or "").strip()
                and str(row.get("source_url") or "").strip()
            ]
        videos = videos[: max(1, args.limit)]
    else:
        videos = collector.collect_channel(args.channel, max_items=max(1, args.limit))
    collect_seconds = time.perf_counter() - collect_started
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
        print(f"[{index}/{len(videos)}] captions: {video.video_id}", flush=True)
        caption_started = time.perf_counter()
        try:
            caption = subtitles.acquire_leading_captions(video, leading_seconds=args.seconds)
            record["caption_seconds"] = round(time.perf_counter() - caption_started, 3)
            record["caption_source"] = caption.source_kind
            record["caption_chars"] = len(caption.text)
            record["caption_status"] = "ready" if not caption.asr_required else "missing"
            if not caption.asr_required:
                print(f"  caption ready in {record['caption_seconds']}s ({caption.source_kind})", flush=True)
                records.append(record)
                continue
        except Exception as exc:  # noqa: BLE001
            record["caption_seconds"] = round(time.perf_counter() - caption_started, 3)
            record["caption_status"] = "error"
            record["error"] = f"caption:{type(exc).__name__}"

        print("  no direct caption; downloading leading audio only", flush=True)
        try:
            log_offset = log_path.stat().st_size if log_path.is_file() else 0
            audio_started = time.perf_counter()
            if args.force_compat:
                # Benchmark-only switch: make the normal service take its existing
                # compatibility fallback without changing production behavior.
                audio._mark_direct_route_failure("configured-proxy")  # noqa: SLF001
                audio._mark_direct_route_failure("configured-proxy")  # noqa: SLF001
                audio._mark_direct_route_failure("direct")  # noqa: SLF001
            result = audio.download_audio(
                video.source_url,
                max_duration_seconds=args.seconds,
                concurrency=max(1, args.audio_concurrency),
            )
            record["audio_seconds"] = round(time.perf_counter() - audio_started, 3)
            record["audio_status"] = "downloaded"
            record["audio_path"] = result.local_path
            route, reason = _route_from_log(log_path, log_offset)
            record["audio_route"] = route
            record["audio_fallback_reason"] = reason
            print(f"  audio downloaded in {record['audio_seconds']}s ({route})", flush=True)
        except Exception as exc:  # noqa: BLE001
            record["audio_seconds"] = round(time.perf_counter() - audio_started, 3)
            record["audio_status"] = "error"
            record["error"] = f"{record['error']} | audio:{type(exc).__name__}".strip(" |")
            print(f"  audio failed: {type(exc).__name__}", flush=True)
        records.append(record)

    fields = list(records[0].keys()) if records else ["index", "video_id"]
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)
    summary = {
        "channel": args.channel,
        "limit": args.limit,
        "seconds": args.seconds,
        "audio_concurrency": max(1, args.audio_concurrency),
        "force_compat": bool(args.force_compat),
        "compat_segment_seconds": audio._COMPAT_SEGMENT_SECONDS,  # noqa: SLF001
        "collect_seconds": round(collect_seconds, 3),
        "total_seconds": round(time.perf_counter() - collect_started, 3),
        "records": records,
    }
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"csv={csv_path.resolve()}")
    print(f"json={json_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
