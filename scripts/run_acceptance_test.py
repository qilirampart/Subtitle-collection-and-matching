"""Run live, non-destructive acceptance checks for the YouTube workflow.

The script uses public media and writes only a timestamped report below
``output/test_runs``.  API secrets are always read from existing runtime
configuration or environment variables and are never written to the report.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.models import YouTubeVideo
from app.services.matching_api import (
    DEFAULT_MATCHING_SERVICE_URL,
    DramaSubtitleMatchingClient,
    MatchingServiceConfig,
    aggregate_video_results,
)
from app.services.subtitle_excel_exporter import export_subtitles_to_xlsx
from app.services.youtube_asr import YouTubeAsrService
from app.services.youtube_audio_service import YouTubeAudioService
from app.services.youtube_collector import YouTubeCollector
from app.services.youtube_cover_review_service import YouTubeCoverReviewService
from app.services.youtube_cover_service import YouTubeCoverService
from app.services.youtube_service import YouTubeService
from app.workflow import VerificationWorkflow


DEFAULT_CHANNEL = "https://www.youtube.com/@Bound2Drama"
DEFAULT_VIDEO = "https://www.youtube.com/watch?v=6lVBMBl8QGw"


class AcceptanceRunner:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.results: list[dict[str, Any]] = []

    def check(self, name: str, callback: Callable[[], dict[str, Any]]) -> dict[str, Any] | None:
        started = time.monotonic()
        try:
            detail = callback()
        except Exception as exc:  # noqa: BLE001 - report every live integration error
            entry = {
                "name": name,
                "status": "failed",
                "elapsed_seconds": round(time.monotonic() - started, 2),
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
            self.results.append(entry)
            print(f"[FAIL] {name}: {entry['error_type']}: {entry['error']}")
            return None
        entry = {
            "name": name,
            "status": "passed",
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "detail": detail,
        }
        self.results.append(entry)
        print(f"[PASS] {name}: {entry['elapsed_seconds']}s")
        return detail

    def write_report(self, *, channel_url: str, video_url: str, seconds: int) -> Path:
        report = {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "source": {
                "channel_url": channel_url,
                "video_url": video_url,
                "leading_seconds": seconds,
            },
            "summary": {
                "passed": sum(item["status"] == "passed" for item in self.results),
                "failed": sum(item["status"] == "failed" for item in self.results),
                "total": len(self.results),
            },
            "checks": self.results,
        }
        report_path = self.output_dir / "acceptance_result.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

        lines = [
            "# YouTube 字幕核验助手验收结果",
            "",
            f"- 测试时间：{report['generated_at']}",
            f"- 样本频道：{channel_url}",
            f"- 样本视频：{video_url}",
            f"- 字幕/下载范围：前 {seconds} 秒",
            f"- 结果：{report['summary']['passed']}/{report['summary']['total']} 通过",
            "",
            "| 链路 | 结果 | 耗时 | 说明 |",
            "| --- | --- | ---: | --- |",
        ]
        for item in self.results:
            detail = item.get("detail") if isinstance(item.get("detail"), dict) else {}
            description = item.get("error") or detail.get("summary") or detail.get("path") or detail.get("status") or "通过"
            lines.append(
                f"| {item['name']} | {'通过' if item['status'] == 'passed' else '失败'} | "
                f"{item['elapsed_seconds']}s | {str(description).replace('|', '/')} |"
            )
        markdown_path = self.output_dir / "acceptance_result.md"
        markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return markdown_path


def _video_from_dict(data: dict[str, object]) -> YouTubeVideo:
    return YouTubeVideo(
        video_id=str(data.get("video_id") or ""),
        source_url=str(data.get("source_url") or ""),
        title=str(data.get("title") or ""),
        channel=str(data.get("channel") or ""),
        upload_date=str(data.get("upload_date") or ""),
        duration_seconds=int(data.get("duration_seconds") or 0),
        thumbnail_url=str(data.get("thumbnail_url") or ""),
    )


def _video_id_from_url(value: str) -> str:
    parts = urlsplit(value)
    if parts.netloc.lower() == "youtu.be":
        return parts.path.strip("/")
    if parts.path.startswith("/shorts/"):
        return parts.path.removeprefix("/shorts/").strip("/")
    return str(parse_qs(parts.query).get("v", [""])[0]).strip()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run live YouTube workflow acceptance checks.")
    parser.add_argument("--channel", default=DEFAULT_CHANNEL)
    parser.add_argument("--video", default=DEFAULT_VIDEO)
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--matching-url", default=DEFAULT_MATCHING_SERVICE_URL)
    parser.add_argument("--skip-matching", action="store_true")
    args = parser.parse_args()

    seconds = max(30, min(int(args.seconds), 180))
    run_dir = Path("output") / "test_runs" / datetime.now().strftime("acceptance_%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=False)
    runner = AcceptanceRunner(run_dir)

    workflow = VerificationWorkflow()
    collected = runner.check(
        "频道采集",
        lambda: (lambda rows: {"summary": f"采集到 {len(rows)} 条视频", "videos": rows})(
            workflow.collect_channel(args.channel, max_items=2)
        ),
    )

    video = YouTubeVideo(_video_id_from_url(args.video), args.video, "验收样本")
    collected_rows = collected.get("videos", []) if collected else []
    batch_videos = [_video_from_dict(item) for item in collected_rows if isinstance(item, dict)]
    if collected_rows:
        first = _video_from_dict(collected_rows[0])
        if first.source_url == args.video or first.video_id in args.video:
            video = first

    video_download = runner.check(
        "视频下载",
        lambda: (lambda result: {"path": result.local_path, "summary": Path(result.local_path).name})(
            YouTubeService().download_video(args.video, max_duration_seconds=seconds)
        ),
    )
    audio_download = runner.check(
        "音频下载",
        lambda: (lambda result: {"path": result.local_path, "summary": Path(result.local_path).name})(
            YouTubeAudioService().download_audio(args.video, max_duration_seconds=seconds, concurrency=3)
        ),
    )
    caption = runner.check(
        "直出字幕获取",
        lambda: (lambda item: {
            "summary": f"{item['status']}，{len(str(item.get('text') or ''))} 字，来源 {item.get('source_kind')}",
            "caption": item,
        })(workflow.inspect_video(video, leading_seconds=seconds)),
    )

    ready_items: list[dict[str, object]] = []
    batch_ready_items: list[dict[str, object]] = []
    if caption and isinstance(caption.get("caption"), dict):
        caption_data = caption["caption"]
        if caption_data.get("status") == "ready_for_matching":
            ready_items, _pending = workflow.prepare_batch_items([video], leading_seconds=seconds)
            runner.check(
                "字幕切段",
                lambda: {"summary": f"生成 {len(ready_items)} 个匹配片段"} if ready_items else (_ for _ in ()).throw(RuntimeError("字幕未生成匹配片段")),
            )
            runner.check(
                "字幕 Excel 导出",
                lambda: (lambda count: {"path": str(run_dir / "subtitles.xlsx"), "summary": f"导出 {count} 条完整字幕"})(
                    export_subtitles_to_xlsx(run_dir / "subtitles.xlsx", ready_items, {video.video_id})
                ),
            )

    if len(batch_videos) >= 2:
        def batch_caption_check() -> dict[str, Any]:
            ready, pending = workflow.prepare_batch_items(batch_videos, leading_seconds=seconds)
            batch_ready_items.extend(ready)
            return {
                "summary": f"{len(batch_videos)} 条视频：{len(ready)} 个字幕片段，{len(pending)} 条待 ASR",
                "status": "completed",
            }

        runner.check("批量字幕准备", batch_caption_check)

    cover = runner.check(
        "封面下载",
        lambda: (lambda result: {"path": result.path, "summary": Path(result.path).name})(
            _require_cover(YouTubeCoverService().download_cover(video, output_dir=run_dir / "covers"))
        ),
    )
    if cover:
        runner.check(
            "封面模型检测",
            lambda: (lambda result: {
                "summary": f"{result.overall_risk}，置信度 {result.confidence}",
                "status": result.overall_risk,
            })(_require_cover_review(YouTubeCoverReviewService().review_cover(video, str(cover["path"])))),
        )

    if len(batch_videos) >= 2:
        batch_cover_paths: dict[str, str] = {}

        def batch_cover_download_check() -> dict[str, Any]:
            results, cancelled = YouTubeCoverService().download_batch(
                batch_videos,
                task_control=None,
            )
            errors = [result.error for result in results if result.error or not result.path]
            if cancelled or errors:
                raise RuntimeError(errors[-1] if errors else "批量封面下载被取消")
            batch_cover_paths.update({result.video_id: result.path for result in results})
            return {"summary": f"成功下载 {len(results)} 张封面"}

        batch_cover = runner.check("批量封面下载", batch_cover_download_check)
        if batch_cover:
            def batch_cover_review_check() -> dict[str, Any]:
                results, cancelled = YouTubeCoverReviewService().review_batch(batch_videos, batch_cover_paths)
                errors = [result.error for result in results if result.error]
                if cancelled or errors:
                    raise RuntimeError(errors[-1] if errors else "批量封面检测被取消")
                return {"summary": f"三路并发检测完成 {len(results)} 张封面"}

            runner.check("批量封面检测", batch_cover_review_check)

    if audio_download:
        runner.check(
            "本地音频 ASR",
            lambda: (lambda result: {"summary": f"识别 {len(result.text)} 字", "path": result.audio_path})(
                _require_asr(YouTubeAsrService()).transcribe_audio_source(str(audio_download["path"]))
            ),
        )

        def asr_fallback_check() -> dict[str, Any]:
            pending = [{
                "video": video.to_dict(),
                "inspection": {
                    "status": "asr_required",
                    "text": "",
                    "language_code": "",
                    "source_kind": "none",
                    "start_seconds": 0,
                    "end_seconds": seconds,
                },
            }]
            fallback_ready, still_pending = workflow.prepare_asr_fallback_items(
                pending,
                leading_seconds=seconds,
                audio_sources={video.video_id: str(audio_download["path"])},
            )
            if still_pending or not fallback_ready:
                raise RuntimeError("ASR 兜底未生成可匹配字幕片段")
            return {"summary": f"ASR 兜底生成 {len(fallback_ready)} 个匹配片段"}

        runner.check("ASR 兜底切段", asr_fallback_check)

    if not args.skip_matching:
        username = os.environ.get("MATCHING_USERNAME", "").strip()
        password = os.environ.get("MATCHING_PASSWORD", "")
        if not username or not password:
            runner.results.append({
                "name": "匹配登录、提交与轮询",
                "status": "failed",
                "elapsed_seconds": 0,
                "error_type": "ConfigurationError",
                "error": "缺少 MATCHING_USERNAME 或 MATCHING_PASSWORD，未执行匹配服务测试。",
            })
            print("[FAIL] 匹配登录、提交与轮询: missing environment credentials")
        elif batch_ready_items or ready_items:
            client = DramaSubtitleMatchingClient(MatchingServiceConfig(args.matching_url, timeout_seconds=90))

            def matching_check() -> dict[str, Any]:
                client.login(username, password)
                batch_items = (batch_ready_items or ready_items)[:4]
                task = client.submit_batch(batch_items, top_k=3)
                task_id = str(task.get("task_id") or "")
                if not task_id:
                    raise RuntimeError("匹配服务未返回 task_id")
                detail = client.wait_for_task(task_id, poll_seconds=1, timeout_seconds=180)
                summaries = aggregate_video_results(detail)
                status = str((detail.get("task") or {}).get("status") or "")
                return {"summary": f"任务 {status}，聚合 {len(summaries)} 条视频", "status": status, "task_id": task_id}

            runner.check("匹配登录、提交与轮询", matching_check)

    report_path = runner.write_report(channel_url=args.channel, video_url=args.video, seconds=seconds)
    summary = runner.results[-1] if runner.results else {}
    print(f"Report: {report_path}")
    return 0 if all(item["status"] == "passed" for item in runner.results) else 1


def _require_cover(result):
    if result.error or not result.path:
        raise RuntimeError(result.error or "封面下载未返回文件")
    return result


def _require_cover_review(result):
    if result.error:
        raise RuntimeError(result.error)
    return result


def _require_asr(service: YouTubeAsrService) -> YouTubeAsrService:
    if not service.is_ready():
        raise RuntimeError("当前 ASR 未配置可用服务商")
    return service


if __name__ == "__main__":
    raise SystemExit(main())
