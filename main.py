from __future__ import annotations

import argparse
import ctypes
import getpass
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from app.models import YouTubeVideo
from app.services.matching_api import DramaSubtitleMatchingClient, MatchingServiceConfig
from app.workflow import VerificationWorkflow


def _set_windows_app_id() -> None:
    """Give source runs a stable taskbar identity instead of pythonw.exe's."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "Dianzhong.YouTubeSubtitleVerifier"
        )
    except (AttributeError, OSError):
        # The GUI remains usable if a restricted Windows environment rejects it.
        pass


def _write_json(payload: object, output_path: str = "") -> None:
    content = json.dumps(payload, ensure_ascii=False, indent=2)
    if output_path:
        Path(output_path).write_text(content, encoding="utf-8")
        return
    print(content)


def _video_from_args(video_url: str, title: str) -> YouTubeVideo:
    parts = urlsplit(video_url)
    video_id = (parse_qs(parts.query).get("v") or [""])[0]
    if not video_id and parts.path.startswith("/shorts/"):
        video_id = parts.path.removeprefix("/shorts/").strip("/")
    if not video_id:
        raise ValueError("Unable to read the YouTube video ID from the URL.")
    return YouTubeVideo(video_id=video_id, source_url=video_url, title=title or video_id)


def main() -> int:
    _set_windows_app_id()
    parser = argparse.ArgumentParser(description="YouTube subtitle verification helper")
    subparsers = parser.add_subparsers(dest="command")

    collect = subparsers.add_parser("collect", help="Collect public videos from a YouTube channel")
    collect.add_argument("channel_url")
    collect.add_argument("--limit", type=int, default=0)
    collect.add_argument("--output", default="")

    inspect = subparsers.add_parser("inspect", help="Download public captions for one video")
    inspect.add_argument("video_url")
    inspect.add_argument("--title", default="")
    inspect.add_argument("--seconds", type=int, choices=(60, 180, 300), default=180)
    inspect.add_argument("--output", default="")

    compare = subparsers.add_parser("compare", help="Inspect captions and call the matching service")
    compare.add_argument("video_url")
    compare.add_argument("--title", default="")
    compare.add_argument("--seconds", type=int, choices=(60, 180, 300), default=180)
    compare.add_argument("--server", required=True)
    compare.add_argument("--username", required=True)
    compare.add_argument("--top-k", type=int, default=10)
    compare.add_argument("--output", default="")

    args = parser.parse_args()
    if args.command is None:
        from app.ui.main_window import run

        return run()
    workflow = VerificationWorkflow()
    if args.command == "collect":
        _write_json(workflow.collect_channel(args.channel_url, max_items=args.limit), args.output)
        return 0

    video = _video_from_args(args.video_url, args.title)
    if args.command == "inspect":
        _write_json(workflow.inspect_video(video, leading_seconds=args.seconds), args.output)
        return 0

    password = getpass.getpass("Matching service password: ")
    client = DramaSubtitleMatchingClient(MatchingServiceConfig(args.server))
    client.login(args.username, password)
    _write_json(
        workflow.compare_video(video, client, leading_seconds=args.seconds, top_k=args.top_k),
        args.output,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
