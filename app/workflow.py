from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from app.models import YouTubeVideo
from app.services.matching_api import DramaSubtitleMatchingClient
from app.services.subtitle_service import YouTubeSubtitleService
from app.services.text_normalizer import SubtitleTextNormalizer
from app.services.youtube_collector import YouTubeCollector
from app.services.youtube_asr import YouTubeAsrService
from app.task_control import TaskControl


class VerificationWorkflow:
    def __init__(
        self,
        *,
        collector: YouTubeCollector | None = None,
        subtitle_service: YouTubeSubtitleService | None = None,
        normalizer: SubtitleTextNormalizer | None = None,
        asr_service: YouTubeAsrService | None = None,
    ) -> None:
        self.collector = collector or YouTubeCollector()
        self.subtitle_service = subtitle_service or YouTubeSubtitleService(self.collector)
        self.normalizer = normalizer or SubtitleTextNormalizer()
        self.asr_service = asr_service or YouTubeAsrService()

    def collect_channel(self, channel_url: str, *, max_items: int = 0) -> list[dict[str, object]]:
        return [video.to_dict() for video in self.collector.collect_channel(channel_url, max_items=max_items)]

    def inspect_video(
        self,
        video: YouTubeVideo,
        *,
        leading_seconds: int = 180,
    ) -> dict[str, object]:
        caption = self.subtitle_service.acquire_leading_captions(video, leading_seconds=leading_seconds)
        normalized_text = self.normalizer.normalize(caption.text, language_code=caption.language_code)
        payload = caption.to_dict()
        payload["normalized_text"] = normalized_text
        payload["matching_language_code"] = self.normalizer.matching_language_code(caption.language_code)
        payload["status"] = "asr_required" if caption.asr_required else "ready_for_matching"
        return payload

    @staticmethod
    def asr_required_inspection(video: YouTubeVideo, leading_seconds: int) -> dict[str, object]:
        """Build the same pending shape without probing a known caption-less video."""
        limit = max(1, int(leading_seconds or 180))
        return {
            "video": video.to_dict(),
            "language_code": "",
            "source_kind": "none",
            "source_path": "",
            "text": "",
            "normalized_text": "",
            "start_seconds": 0,
            "end_seconds": limit,
            "asr_required": True,
            "status": "asr_required",
            "asr_status": "skipped_caption_probe",
        }

    def compare_video(
        self,
        video: YouTubeVideo,
        matching_client: DramaSubtitleMatchingClient,
        *,
        leading_seconds: int = 180,
        top_k: int = 10,
    ) -> dict[str, object]:
        inspection = self.inspect_video(video, leading_seconds=leading_seconds)
        if inspection["status"] == "asr_required":
            inspection = self._try_asr_fallback(video, inspection, leading_seconds=leading_seconds)
            if inspection["status"] == "asr_required":
                return {"status": "asr_required", "video": video.to_dict(), "caption": inspection, "match": None}
        result = matching_client.compare(
            str(inspection["normalized_text"]),
            language_code=str(inspection.get("matching_language_code") or ""),
            top_k=top_k,
        )
        return {
            "status": "completed",
            "video": video.to_dict(),
            "caption": inspection,
            "match": result,
        }

    def _try_asr_fallback(
        self,
        video: YouTubeVideo,
        inspection: dict[str, object],
        *,
        leading_seconds: int,
        audio_source: str = "",
        segment_concurrency: int = 6,
        should_cancel=None,
    ) -> dict[str, object]:
        if not self.asr_service.is_ready():
            inspection["asr_status"] = "not_configured"
            return inspection
        try:
            transcript = (
                self.asr_service.transcribe_audio_source(audio_source, should_cancel=should_cancel)
                if audio_source else self.asr_service.transcribe_video(
                    video.source_url,
                    leading_seconds=leading_seconds,
                    segment_concurrency=segment_concurrency,
                    should_cancel=should_cancel,
                )
            )
        except Exception as exc:  # noqa: BLE001
            inspection["asr_status"] = "failed"
            inspection["asr_error"] = str(exc)
            return inspection
        text = self.normalizer.normalize(transcript.text)
        if not text:
            inspection["asr_status"] = "empty"
            return inspection
        inspection.update(
            {
                "source_kind": "asr",
                "source_path": transcript.source_path,
                "text": transcript.text,
                "normalized_text": text,
                "matching_language_code": "",
                "asr_required": False,
                "status": "ready_for_matching",
                "asr_status": "completed",
            }
        )
        return inspection

    def prepare_batch_items(
        self,
        videos: list[YouTubeVideo],
        *,
        leading_seconds: int = 180,
        audio_sources: dict[str, str] | None = None,
        allow_asr_fallback: bool = False,
        skip_caption_probe: bool = False,
        caption_concurrency: int = 1,
        download_concurrency: int = 1,
        asr_concurrency: int = 1,
        progress_callback=None,
        stage_callback=None,
        task_control: TaskControl | None = None,
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        ready: list[dict[str, object]] = []
        pending_asr: list[dict[str, object]] = []
        caption_workers = max(1, min(int(caption_concurrency or 1), 3))

        def handle_inspection(index: int, video: YouTubeVideo, inspection: dict[str, object]) -> None:
            if inspection["status"] == "asr_required":
                pending_asr.append({"video": video.to_dict(), "inspection": inspection})
            else:
                ready.append(self._build_video_match_item(video, inspection))
            if progress_callback is not None:
                progress_callback(index, len(videos), video, inspection)

        if skip_caption_probe:
            for index, video in enumerate(videos, start=1):
                if task_control is not None and not task_control.checkpoint():
                    break
                handle_inspection(index, video, self.asr_required_inspection(video, leading_seconds))
        elif caption_workers == 1:
            for index, video in enumerate(videos, start=1):
                if task_control is not None and not task_control.checkpoint():
                    break
                handle_inspection(index, video, self.inspect_video(video, leading_seconds=leading_seconds))
        else:
            with ThreadPoolExecutor(max_workers=caption_workers, thread_name_prefix="youtube-caption") as executor:
                futures = {
                    executor.submit(self.inspect_video, video, leading_seconds=leading_seconds): (index, video)
                    for index, video in enumerate(videos, start=1)
                }
                completed = 0
                for future in as_completed(futures):
                    if task_control is not None and not task_control.checkpoint():
                        for pending in futures:
                            pending.cancel()
                        break
                    index, video = futures[future]
                    inspection = future.result()
                    completed += 1
                    handle_inspection(completed, video, inspection)
        if allow_asr_fallback and pending_asr and (task_control is None or not task_control.cancelled):
            asr_ready, pending_asr = self.prepare_asr_fallback_items(
                pending_asr,
                leading_seconds=leading_seconds,
                audio_sources=audio_sources,
                download_concurrency=download_concurrency,
                asr_concurrency=asr_concurrency,
                progress_callback=progress_callback,
                stage_callback=stage_callback,
                task_control=task_control,
            )
            ready.extend(asr_ready)
        return ready, pending_asr

    def prepare_asr_fallback_items(
        self,
        pending_items: list[dict[str, object]],
        *,
        leading_seconds: int = 180,
        audio_sources: dict[str, str] | None = None,
        download_concurrency: int = 1,
        asr_concurrency: int = 1,
        progress_callback=None,
        stage_callback=None,
        task_control: TaskControl | None = None,
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        """Run bounded download and ASR stages, then retry concurrent failures serially."""
        ready: list[dict[str, object]] = []
        still_pending: list[dict[str, object]] = []
        if not self.asr_service.is_ready():
            # Preserve the queue, but annotate it so the UI can distinguish
            # "not configured" from a real download/transcription failure.
            not_configured = []
            for item in pending_items:
                pending = dict(item)
                inspection = dict(pending.get("inspection") or {})
                inspection["asr_status"] = "not_configured"
                inspection["asr_error"] = "ASR 未配置或没有可用的 API 密钥。"
                pending["inspection"] = inspection
                not_configured.append(pending)
            return ready, not_configured

        def checkpoint() -> bool:
            return task_control is None or task_control.checkpoint()

        entries: list[tuple[int, YouTubeVideo, dict[str, object], str]] = []
        for index, item in enumerate(pending_items, start=1):
            if not checkpoint():
                return ready, still_pending + pending_items[index - 1:]
            video_data = item.get("video")
            inspection_data = item.get("inspection")
            if not isinstance(video_data, dict) or not isinstance(inspection_data, dict):
                continue
            video = YouTubeVideo(**video_data)
            entries.append((index, video, dict(inspection_data), str((audio_sources or {}).get(video.video_id) or "")))

        def pending_from_entries() -> list[dict[str, object]]:
            return [
                {"video": video.to_dict(), "inspection": inspection}
                for _index, video, inspection, _audio_source in entries
            ]

        download_workers = max(1, min(int(download_concurrency or 1), 3))
        asr_workers = max(1, min(int(asr_concurrency or 1), 3))
        segment_concurrency = max(1, 6 // download_workers)
        audio_ready: list[tuple[int, YouTubeVideo, dict[str, object], str]] = []
        retry_entries: list[tuple[int, YouTubeVideo, dict[str, object], str]] = []

        def report_stage(text: str) -> None:
            if stage_callback is not None:
                stage_callback(text)

        def download(entry: tuple[int, YouTubeVideo, dict[str, object], str]) -> tuple[int, YouTubeVideo, dict[str, object], str]:
            index, video, inspection, audio_source = entry
            if audio_source:
                return entry
            source = YouTubeAsrService().download_video_audio(
                video.source_url,
                leading_seconds=leading_seconds,
                segment_concurrency=segment_concurrency,
                should_cancel=lambda: not checkpoint(),
            )
            return index, video, inspection, source

        report_stage(f"正在下载音频：0/{len(entries)}（{download_workers} 路）")
        with ThreadPoolExecutor(max_workers=download_workers, thread_name_prefix="youtube-asr-download") as executor:
            futures = {executor.submit(download, entry): entry for entry in entries}
            for future in as_completed(futures):
                entry = futures[future]
                if not checkpoint():
                    for pending in futures:
                        pending.cancel()
                    break
                try:
                    audio_ready.append(future.result())
                except Exception as exc:  # noqa: BLE001
                    index, video, inspection, _ = entry
                    inspection["asr_status"] = "failed"
                    inspection["asr_error"] = str(exc)
                    retry_entries.append((index, video, inspection, ""))
                report_stage(f"正在下载音频：{len(audio_ready) + len(retry_entries)}/{len(entries)}（{download_workers} 路）")

        if not checkpoint():
            return ready, pending_from_entries()

        def transcribe(entry: tuple[int, YouTubeVideo, dict[str, object], str]):
            index, video, inspection, audio_source = entry
            transcript = YouTubeAsrService().transcribe_audio_source(
                audio_source,
                should_cancel=lambda: not checkpoint(),
            )
            return index, video, inspection, audio_source, transcript

        completed = 0
        report_stage(f"正在 ASR 转写：0/{len(audio_ready)}（{asr_workers} 路）")
        with ThreadPoolExecutor(max_workers=asr_workers, thread_name_prefix="youtube-asr-transcribe") as executor:
            futures = {executor.submit(transcribe, entry): entry for entry in audio_ready}
            for future in as_completed(futures):
                entry = futures[future]
                if not checkpoint():
                    for pending in futures:
                        pending.cancel()
                    break
                try:
                    index, video, inspection, _audio_source, transcript = future.result()
                    text = self.normalizer.normalize(transcript.text)
                    if not text:
                        raise RuntimeError("ASR returned an empty transcript.")
                    inspection.update({
                        "source_kind": "asr", "source_path": transcript.source_path,
                        "text": transcript.text, "normalized_text": text,
                        "matching_language_code": "", "asr_required": False,
                        "status": "ready_for_matching", "asr_status": "completed",
                    })
                    ready.append(self._build_video_match_item(video, inspection))
                except Exception as exc:  # noqa: BLE001
                    index, video, inspection, audio_source = entry
                    inspection["asr_status"] = "failed"
                    inspection["asr_error"] = str(exc)
                    retry_entries.append((index, video, inspection, audio_source))
                    continue
                completed += 1
                report_stage(f"正在 ASR 转写：{completed}/{len(entries)}（{asr_workers} 路）")
                if progress_callback is not None:
                    progress_callback(completed, len(entries), video, inspection)

        if not checkpoint():
            ready_ids = {str(item.get("source_video_id") or "") for item in ready}
            return ready, [
                item for item in pending_from_entries()
                if str((item.get("video") or {}).get("video_id") or "") not in ready_ids
            ]

        # A failed concurrent item retries on the proven single-route path.
        if retry_entries:
            report_stage(f"正在以稳定单路重试：0/{len(retry_entries)}")
        for index, video, inspection, audio_source in retry_entries:
            if not checkpoint():
                still_pending.append({"video": video.to_dict(), "inspection": inspection})
                continue
            inspection = self._try_asr_fallback(
                video, inspection, leading_seconds=leading_seconds, audio_source=audio_source,
                segment_concurrency=6, should_cancel=lambda: not checkpoint(),
            )
            completed += 1
            report_stage(f"正在以稳定单路重试：{completed}/{len(entries)}")
            if inspection["status"] == "asr_required":
                still_pending.append({"video": video.to_dict(), "inspection": inspection})
            else:
                ready.append(self._build_video_match_item(video, inspection))
            if progress_callback is not None:
                progress_callback(completed, len(entries), video, inspection)
        return ready, still_pending

    def _build_video_match_item(self, video: YouTubeVideo, inspection: dict[str, object]) -> dict[str, object]:
        """Keep one complete subtitle record per video; the service owns fallback slicing."""
        raw_cues = inspection.get("cues")
        cues = [
            {
                "start_seconds": float(cue.get("start_seconds") or 0),
                "end_seconds": float(cue.get("end_seconds") or cue.get("start_seconds") or 0),
                "text": str(cue.get("text") or "").strip(),
            }
            for cue in raw_cues
            if isinstance(cue, dict) and str(cue.get("text") or "").strip()
        ] if isinstance(raw_cues, (list, tuple)) else []
        full_text = str(inspection.get("text") or "").strip()
        if not cues:
            cues = [{
                "start_seconds": float(inspection.get("start_seconds") or 0),
                "end_seconds": float(inspection.get("end_seconds") or 0),
                "text": full_text,
            }]
        start = float(cues[0]["start_seconds"] or 0)
        end = float(cues[-1]["end_seconds"] or start)
        return {
            "source_ref": video.source_url,
            "query_text": str(inspection.get("normalized_text") or full_text),
            "source_platform": "YouTube",
            "source_display_title": video.title,
            "source_description": video.source_url,
            "source_video_id": video.video_id,
            "source_channel": video.channel,
            "source_upload_date": video.upload_date,
            "source_caption_language": inspection.get("language_code", ""),
            "source_caption_source": inspection.get("source_kind", ""),
            "source_caption_start": inspection.get("start_seconds", 0),
            "source_caption_end": inspection.get("end_seconds", 0),
            "source_caption_text": full_text,
            "source_segment_order": 1,
            "source_time_start": f"{start:.3f}",
            "source_time_end": f"{end:.3f}",
            "source_text_original": full_text,
            "cues": cues,
            "matching_mode": "video",
        }

    def coalesce_video_match_items(self, items: list[dict[str, object]]) -> list[dict[str, object]]:
        """Upgrade legacy local segment records into one complete record per video."""
        grouped: dict[str, list[dict[str, object]]] = {}
        for index, raw_item in enumerate(items, start=1):
            if not isinstance(raw_item, dict):
                continue
            item = dict(raw_item)
            key = str(item.get("source_video_id") or item.get("source_ref") or f"item-{index}").strip()
            key = key.split("#segment-", 1)[0] or f"item-{index}"
            grouped.setdefault(key, []).append(item)

        def as_float(value: object) -> float:
            try:
                return float(value)
            except (TypeError, ValueError):
                return 0.0

        def segment_order(value: dict[str, object]) -> int:
            try:
                return int(value.get("source_segment_order") or 0)
            except (TypeError, ValueError):
                return 0

        normalized: list[dict[str, object]] = []
        for group in grouped.values():
            group.sort(key=segment_order)
            result = dict(group[0])
            caption_text = max(
                (str(value.get("source_caption_text") or "").strip() for value in group),
                key=len,
                default="",
            )
            if not caption_text:
                caption_text = "\n".join(
                    str(value.get("source_text_original") or value.get("query_text") or "").strip()
                    for value in group
                    if str(value.get("source_text_original") or value.get("query_text") or "").strip()
                )
            language_code = str(result.get("source_caption_language") or "")
            cues: list[dict[str, object]] = []
            seen_cues: set[tuple[float, float, str]] = set()
            for item in group:
                raw_cues = item.get("cues") if isinstance(item.get("cues"), list) else []
                if not raw_cues:
                    raw_cues = [{
                        "start_seconds": item.get("source_time_start"),
                        "end_seconds": item.get("source_time_end"),
                        "text": item.get("source_text_original") or item.get("query_text"),
                    }]
                for cue in raw_cues:
                    if not isinstance(cue, dict):
                        continue
                    text = str(cue.get("text") or "").strip()
                    if not text:
                        continue
                    start = as_float(cue.get("start_seconds"))
                    end = as_float(cue.get("end_seconds"))
                    signature = (start, end, text)
                    if signature not in seen_cues:
                        seen_cues.add(signature)
                        cues.append({"start_seconds": start, "end_seconds": end, "text": text})
            cues.sort(key=lambda cue: (float(cue["start_seconds"]), float(cue["end_seconds"])))
            if not cues and caption_text:
                cues = [{"start_seconds": 0.0, "end_seconds": 0.0, "text": caption_text}]

            result.update(
                {
                    "source_ref": str(result.get("source_ref") or "").split("#segment-", 1)[0],
                    "query_text": self.normalizer.normalize(caption_text, language_code=language_code),
                    "source_caption_text": caption_text,
                    "source_text_original": caption_text,
                    "source_segment_order": 1,
                    "source_time_start": f"{float(cues[0]['start_seconds']) if cues else 0.0:.3f}",
                    "source_time_end": f"{float(cues[-1]['end_seconds']) if cues else 0.0:.3f}",
                    "cues": cues,
                    "matching_mode": "video",
                }
            )
            if str(result.get("query_text") or "").strip():
                normalized.append(result)
        return normalized

    @staticmethod
    def filter_video_match_items(
        items: list[dict[str, object]],
        selected_video_ids: set[str],
    ) -> list[dict[str, object]]:
        """Keep only subtitle records belonging to the videos checked in the queue."""
        selected_ids = {str(video_id or "").strip() for video_id in selected_video_ids}
        selected_ids.discard("")
        if not selected_ids:
            return []
        return [
            dict(item)
            for item in items
            if isinstance(item, dict)
            and str(item.get("source_video_id") or "").strip() in selected_ids
        ]

    # Kept as a private compatibility shim for the independent subtitle page.
    def _build_batch_items(self, video: YouTubeVideo, inspection: dict[str, object]) -> list[dict[str, object]]:
        return [self._build_video_match_item(video, inspection)]
