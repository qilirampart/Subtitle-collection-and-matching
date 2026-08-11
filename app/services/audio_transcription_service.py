from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import time
from pathlib import Path
from typing import Any, Callable

import requests

from app.config.settings import ASR_AUDIO_CHUNK_SECONDS, ASR_DIRECT_UPLOAD_LIMIT_BYTES, EXTRACTED_AUDIO_DIR
from app.audio_transcription_models import (
    AudioTranscriptionResult,
    PreparedAudio,
    TranscriptSegment,
    TranscriptWord,
)
from app.services.api_config_service import ApiConfigService
from app.services.doubao_asr_client import DoubaoAsrClient
from app.utils.failover import FailoverRouter
from app.utils.ffmpeg import extract_audio_track, probe_media_duration_ms
from app.utils.logger import get_logger
from app.utils.paths import next_compact_name

ProgressCallback = Callable[[int, int, str], None]
CancelCallback = Callable[[], bool]


class AudioTranscriptionError(RuntimeError):
    pass


class AudioTranscriptionService:
    tencent_host = "asr.tencentcloudapi.com"
    tencent_endpoint = "https://asr.tencentcloudapi.com"
    tencent_service = "asr"
    tencent_version = "2019-06-14"

    def __init__(self) -> None:
        self._api_config_service = ApiConfigService()
        self._failover_router = FailoverRouter("asr", logger_name=__name__)
        self._doubao_client = DoubaoAsrClient()
        self._logger = get_logger(__name__)

    def extract_audio(
        self,
        source_path: str,
        *,
        progress_callback: ProgressCallback | None = None,
        should_cancel: CancelCallback | None = None,
    ) -> PreparedAudio:
        source = Path(source_path)
        if not source.exists():
            raise AudioTranscriptionError(f"源文件不存在: {source}")

        self._check_cancel(should_cancel)
        self._emit_progress(progress_callback, 0, 3, "开始提取音频...")

        EXTRACTED_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        audio_stem = next_compact_name("audio")
        audio_path = EXTRACTED_AUDIO_DIR / f"{audio_stem}.mp3"

        extract_audio_track(source, audio_path)
        self._check_cancel(should_cancel)
        self._emit_progress(progress_callback, 1, 3, "音频提取完成，正在分析时长...")

        duration_ms = probe_media_duration_ms(audio_path)
        size_bytes = audio_path.stat().st_size

        chunk_paths: list[str] = []
        chunk_offsets_ms: list[int] = []

        chunk_duration_ms = ASR_AUDIO_CHUNK_SECONDS * 1000
        if size_bytes <= ASR_DIRECT_UPLOAD_LIMIT_BYTES:
            chunk_paths.append(str(audio_path))
            chunk_offsets_ms.append(0)
        else:
            for index, start_ms in enumerate(range(0, max(duration_ms, 1), chunk_duration_ms), start=1):
                self._check_cancel(should_cancel)
                chunk_path = EXTRACTED_AUDIO_DIR / f"{audio_stem}_p{index:02d}.mp3"
                extract_audio_track(
                    audio_path,
                    chunk_path,
                    start_ms=start_ms,
                    duration_ms=min(chunk_duration_ms, duration_ms - start_ms),
                )
                chunk_paths.append(str(chunk_path))
                chunk_offsets_ms.append(start_ms)

        self._emit_progress(progress_callback, 3, 3, "音频准备完成。")
        return PreparedAudio(
            source_path=str(source),
            audio_path=str(audio_path),
            duration_ms=duration_ms,
            size_bytes=size_bytes,
            chunk_paths=chunk_paths,
            chunk_offsets_ms=chunk_offsets_ms,
        )

    def transcribe_prepared_audio(
        self,
        prepared: PreparedAudio,
        *,
        progress_callback: ProgressCallback | None = None,
        should_cancel: CancelCallback | None = None,
    ) -> AudioTranscriptionResult:
        api_config = self._api_config_service.load_config()
        asr_section = api_config.get("asr", {})
        if not asr_section.get("enabled", True):
            raise AudioTranscriptionError("ASR 总开关已关闭，请先在配置里启用。")

        providers = self._api_config_service.list_asr_providers(require_secret=True)
        if not providers:
            raise AudioTranscriptionError("没有可用的 ASR 提供商，请先补齐腾讯云或豆包的配置。")

        failover_policy = asr_section.get("failover", {})
        failure_threshold = max(1, int(failover_policy.get("failure_threshold", 1) or 1))
        cooldown_seconds = max(0, int(failover_policy.get("cooldown_seconds", 300) or 300))

        segments: list[TranscriptSegment] = []
        raw_tasks: list[dict[str, Any]] = []
        total_chunks = max(len(prepared.chunk_paths), 1)

        self._logger.info(
            "Starting audio transcription. source=%s chunks=%s asr_providers=%s",
            prepared.source_path,
            total_chunks,
            len(providers),
        )

        for index, chunk_path in enumerate(prepared.chunk_paths, start=1):
            self._check_cancel(should_cancel)
            self._emit_progress(progress_callback, index - 1, total_chunks, f"开始识别第 {index}/{total_chunks} 段...")
            provider_name, provider_type, task_data, chunk_segments = self._transcribe_chunk_with_failover(
                Path(chunk_path),
                providers,
                failure_threshold=failure_threshold,
                cooldown_seconds=cooldown_seconds,
                should_cancel=should_cancel,
            )
            raw_tasks.append(
                {
                    "provider_name": provider_name,
                    "provider_type": provider_type,
                    "task_data": task_data,
                }
            )
            offset_ms = prepared.chunk_offsets_ms[index - 1]
            segments.extend(self._offset_segments(chunk_segments, offset_ms=offset_ms))
            self._emit_progress(
                progress_callback,
                index,
                total_chunks,
                f"第 {index}/{total_chunks} 段识别完成（ASR: {provider_name}）",
            )

        segments.sort(key=lambda item: (item.start_ms, item.end_ms))
        merged_text = "".join(segment.text for segment in segments).strip()
        srt_text = self._build_srt(segments)
        self._logger.info(
            "Audio transcription completed. source=%s segments=%s",
            prepared.source_path,
            len(segments),
        )
        return AudioTranscriptionResult(
            source_path=prepared.source_path,
            audio_path=prepared.audio_path,
            text=merged_text,
            srt_text=srt_text,
            segments=segments,
            raw_tasks=raw_tasks,
        )

    def transcribe_source(
        self,
        source_path: str,
        *,
        progress_callback: ProgressCallback | None = None,
        should_cancel: CancelCallback | None = None,
    ) -> tuple[PreparedAudio, AudioTranscriptionResult]:
        prepared = self.extract_audio(
            source_path,
            progress_callback=progress_callback,
            should_cancel=should_cancel,
        )
        result = self.transcribe_prepared_audio(
            prepared,
            progress_callback=progress_callback,
            should_cancel=should_cancel,
        )
        return prepared, result

    def transcribe_source_with_provider(
        self,
        source_path: str,
        provider: dict[str, object],
        *,
        progress_callback: ProgressCallback | None = None,
        should_cancel: CancelCallback | None = None,
    ) -> tuple[PreparedAudio, AudioTranscriptionResult]:
        prepared = self.extract_audio(
            source_path,
            progress_callback=progress_callback,
            should_cancel=should_cancel,
        )
        result = self.transcribe_prepared_audio_with_provider(
            prepared,
            provider,
            progress_callback=progress_callback,
            should_cancel=should_cancel,
        )
        return prepared, result

    def transcribe_prepared_audio_with_provider(
        self,
        prepared: PreparedAudio,
        provider: dict[str, object],
        *,
        progress_callback: ProgressCallback | None = None,
        should_cancel: CancelCallback | None = None,
    ) -> AudioTranscriptionResult:
        provider_payload = dict(provider)
        provider_name = str(provider_payload.get("name") or "ASR").strip() or "ASR"
        provider_type = str(provider_payload.get("provider") or "tencent_asr").strip() or "tencent_asr"
        if not self._api_config_service.is_asr_provider_ready(provider_payload):
            raise AudioTranscriptionError(f"ASR 配置不完整，无法检测：{provider_name}")

        segments: list[TranscriptSegment] = []
        raw_tasks: list[dict[str, Any]] = []
        total_chunks = max(len(prepared.chunk_paths), 1)

        self._logger.info(
            "Starting provider probe transcription. source=%s provider=%s type=%s chunks=%s",
            prepared.source_path,
            provider_name,
            provider_type,
            total_chunks,
        )

        for index, chunk_path in enumerate(prepared.chunk_paths, start=1):
            self._check_cancel(should_cancel)
            self._emit_progress(
                progress_callback,
                index - 1,
                total_chunks,
                f"正在检测 {provider_name}，处理音频分段 {index}/{total_chunks}...",
            )
            task_data, chunk_segments = self._transcribe_chunk_with_provider(
                Path(chunk_path),
                provider_payload,
                should_cancel=should_cancel,
            )
            raw_tasks.append(
                {
                    "provider_name": provider_name,
                    "provider_type": provider_type,
                    "task_data": task_data,
                }
            )
            offset_ms = prepared.chunk_offsets_ms[index - 1]
            segments.extend(self._offset_segments(chunk_segments, offset_ms=offset_ms))
            self._emit_progress(
                progress_callback,
                index,
                total_chunks,
                f"第 {index}/{total_chunks} 段识别完成（ASR: {provider_name}）",
            )

        segments.sort(key=lambda item: (item.start_ms, item.end_ms))
        merged_text = "".join(segment.text for segment in segments).strip()
        srt_text = self._build_srt(segments)
        self._logger.info(
            "Provider probe transcription completed. source=%s provider=%s segments=%s",
            prepared.source_path,
            provider_name,
            len(segments),
        )
        return AudioTranscriptionResult(
            source_path=prepared.source_path,
            audio_path=prepared.audio_path,
            text=merged_text,
            srt_text=srt_text,
            segments=segments,
            raw_tasks=raw_tasks,
        )

    def _transcribe_chunk_with_failover(
        self,
        audio_path: Path,
        providers: list[dict[str, object]],
        *,
        failure_threshold: int,
        cooldown_seconds: int,
        should_cancel: CancelCallback | None = None,
    ) -> tuple[str, str, dict[str, Any], list[TranscriptSegment]]:
        errors: list[str] = []
        ordered_providers = self._failover_router.ordered_candidates(
            providers,
            failure_threshold=failure_threshold,
            cooldown_seconds=cooldown_seconds,
        )

        for provider in ordered_providers:
            self._check_cancel(should_cancel)
            provider_name = str(provider.get("name") or "ASR")
            provider_type = str(provider.get("provider") or "tencent_asr")
            try:
                task_data, chunk_segments = self._transcribe_chunk_with_provider(
                    audio_path,
                    provider,
                    should_cancel=should_cancel,
                )
            except Exception as exc:  # noqa: BLE001
                if should_cancel is not None and should_cancel():
                    raise
                error_message = str(exc)
                self._logger.warning(
                    "ASR provider failed. provider=%s type=%s audio=%s error=%s",
                    provider_name,
                    provider_type,
                    audio_path.name,
                    error_message,
                )
                self._failover_router.record_failure(
                    provider,
                    error_message,
                    failure_threshold=failure_threshold,
                    cooldown_seconds=cooldown_seconds,
                )
                errors.append(f"{provider_name}: {error_message}")
                continue

            self._failover_router.record_success(provider)
            self._logger.info(
                "ASR provider succeeded. provider=%s type=%s audio=%s",
                provider_name,
                provider_type,
                audio_path.name,
            )
            return provider_name, provider_type, task_data, chunk_segments

        detail = "；".join(errors[:3])
        if len(errors) > 3:
            detail += "；其余 ASR 节点也已失败"
        raise AudioTranscriptionError(f"所有 ASR 提供商都不可用。{detail}")

    def _transcribe_chunk_with_provider(
        self,
        audio_path: Path,
        provider: dict[str, object],
        *,
        should_cancel: CancelCallback | None = None,
    ) -> tuple[dict[str, Any], list[TranscriptSegment]]:
        provider_type = str(provider.get("provider") or "tencent_asr")
        if provider_type == "doubao_asr":
            task_data = self._transcribe_doubao_chunk(audio_path, provider, should_cancel=should_cancel)
            return task_data, self._parse_doubao_segments(task_data)

        if provider_type != "tencent_asr":
            raise AudioTranscriptionError(f"不支持的 ASR Provider: {provider_type}")

        task_id = self._create_tencent_task(audio_path, provider)
        task_data = self._poll_tencent_task(task_id, provider, should_cancel=should_cancel)
        return task_data, self._parse_tencent_segments(task_data)

    def _create_tencent_task(self, audio_path: Path, config: dict[str, object]) -> int:
        audio_bytes = audio_path.read_bytes()
        payload = {
            "EngineModelType": config["engine_model_type"],
            "ChannelNum": int(config["channel_num"]),
            "ResTextFormat": int(config["res_text_format"]),
            "SourceType": 1,
            "Data": base64.b64encode(audio_bytes).decode("ascii"),
            "DataLen": len(audio_bytes),
        }
        response = self._signed_tencent_request("CreateRecTask", payload, config)
        parsed = self._parse_tencent_response(response)
        data = parsed.get("Response", {}).get("Data") or {}
        task_id = data.get("TaskId")
        if task_id is None:
            raise AudioTranscriptionError("未获取到腾讯云任务 ID。")
        return int(task_id)

    def _poll_tencent_task(
        self,
        task_id: int,
        config: dict[str, object],
        *,
        should_cancel: CancelCallback | None = None,
        interval_seconds: float = 3.0,
        max_attempts: int = 120,
    ) -> dict[str, Any]:
        for _attempt in range(max_attempts):
            self._check_cancel(should_cancel)
            response = self._signed_tencent_request("DescribeTaskStatus", {"TaskId": task_id}, config)
            parsed = self._parse_tencent_response(response)
            data = parsed.get("Response", {}).get("Data") or {}
            status = int(data.get("Status", -1))
            if status == 2:
                return data
            if status == 3:
                raise AudioTranscriptionError(data.get("ErrorMsg") or "腾讯云语音识别任务失败。")
            deadline = time.monotonic() + max(0.1, interval_seconds)
            while time.monotonic() < deadline:
                self._check_cancel(should_cancel)
                time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
        raise AudioTranscriptionError("腾讯云语音识别轮询超时。")

    def _signed_tencent_request(
        self,
        action: str,
        body: dict[str, object],
        config: dict[str, object],
    ) -> requests.Response:
        payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        timestamp = int(time.time())
        date = dt.datetime.utcfromtimestamp(timestamp).strftime("%Y-%m-%d")
        credential_scope = f"{date}/{self.tencent_service}/tc3_request"
        hashed_payload = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        canonical_request = "\n".join(
            [
                "POST",
                "/",
                "",
                f"content-type:application/json; charset=utf-8\nhost:{self.tencent_host}\n",
                "content-type;host",
                hashed_payload,
            ]
        )
        string_to_sign = "\n".join(
            [
                "TC3-HMAC-SHA256",
                str(timestamp),
                credential_scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )
        secret_date = self._sign(("TC3" + str(config["secret_key"])).encode("utf-8"), date)
        secret_service = self._sign(secret_date, self.tencent_service)
        secret_signing = self._sign(secret_service, "tc3_request")
        signature = hmac.new(secret_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        authorization = (
            "TC3-HMAC-SHA256 "
            f"Credential={config['secret_id']}/{credential_scope}, "
            "SignedHeaders=content-type;host, "
            f"Signature={signature}"
        )
        headers = {
            "Authorization": authorization,
            "Content-Type": "application/json; charset=utf-8",
            "Host": self.tencent_host,
            "X-TC-Action": action,
            "X-TC-Timestamp": str(timestamp),
            "X-TC-Version": self.tencent_version,
            "X-TC-Region": str(config["region"]),
        }
        session = requests.Session()
        session.trust_env = False
        try:
            return session.post(
                self.tencent_endpoint,
                headers=headers,
                data=payload.encode("utf-8"),
                timeout=60,
            )
        except Exception as exc:  # noqa: BLE001
            raise AudioTranscriptionError(f"请求腾讯云失败: {exc}") from exc

    @staticmethod
    def _parse_tencent_response(response: requests.Response) -> dict[str, Any]:
        try:
            parsed = response.json()
        except Exception as exc:  # noqa: BLE001
            raise AudioTranscriptionError(f"腾讯云返回了非 JSON 内容: {response.text[:500]}") from exc
        error = parsed.get("Response", {}).get("Error")
        if error:
            code = str(error.get("Code") or "").strip()
            message = str(error.get("Message") or "").strip()
            raise AudioTranscriptionError(f"{code}: {message}" if code else message)
        return parsed

    def _transcribe_doubao_chunk(
        self,
        audio_path: Path,
        config: dict[str, object],
        *,
        should_cancel: CancelCallback | None = None,
    ) -> dict[str, Any]:
        try:
            return self._doubao_client.transcribe_file(audio_path, config, should_cancel=should_cancel)
        except Exception as exc:  # noqa: BLE001
            raise AudioTranscriptionError(f"豆包 ASR 识别失败: {exc}") from exc

    @staticmethod
    def _parse_tencent_segments(task_data: dict[str, Any]) -> list[TranscriptSegment]:
        items = task_data.get("ResultDetail") or []
        segments: list[TranscriptSegment] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            words: list[TranscriptWord] = []
            for raw_word in item.get("Words") or []:
                if not isinstance(raw_word, dict):
                    continue
                words.append(
                    TranscriptWord(
                        text=str(raw_word.get("Word") or "").strip(),
                        start_ms=int(raw_word.get("OffsetStartMs") or 0),
                        end_ms=int(raw_word.get("OffsetEndMs") or 0),
                    )
                )
            text = str(item.get("FinalSentence") or item.get("SliceSentence") or "").strip()
            if not text:
                continue
            segments.append(
                TranscriptSegment(
                    text=text,
                    start_ms=int(item.get("StartMs") or 0),
                    end_ms=int(item.get("EndMs") or 0),
                    speaker_id=int(item.get("SpeakerId") or 0),
                    words=words,
                )
            )
        return segments

    @staticmethod
    def _parse_doubao_segments(task_data: dict[str, Any]) -> list[TranscriptSegment]:
        result = task_data.get("result") or {}
        utterances = result.get("utterances") or []
        segments: list[TranscriptSegment] = []

        for item in utterances:
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            words: list[TranscriptWord] = []
            for raw_word in item.get("words") or []:
                if not isinstance(raw_word, dict):
                    continue
                word_text = str(raw_word.get("text") or raw_word.get("word") or "").strip()
                if not word_text:
                    continue
                words.append(
                    TranscriptWord(
                        text=word_text,
                        start_ms=int(raw_word.get("start_time") or 0),
                        end_ms=int(raw_word.get("end_time") or raw_word.get("start_time") or 0),
                    )
                )
            start_ms = int(item.get("start_time") or 0)
            end_ms = int(item.get("end_time") or start_ms)
            segments.append(
                TranscriptSegment(
                    text=text,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    speaker_id=0,
                    words=words,
                )
            )

        if segments:
            return segments

        fallback_text = str(result.get("text") or "").strip()
        if not fallback_text:
            return []
        duration_ms = int((task_data.get("audio_info") or {}).get("duration") or 0)
        return [
            TranscriptSegment(
                text=fallback_text,
                start_ms=0,
                end_ms=max(duration_ms, 0),
            )
        ]

    @staticmethod
    def _offset_segments(segments: list[TranscriptSegment], *, offset_ms: int) -> list[TranscriptSegment]:
        adjusted: list[TranscriptSegment] = []
        for segment in segments:
            adjusted.append(
                TranscriptSegment(
                    text=segment.text,
                    start_ms=offset_ms + int(segment.start_ms),
                    end_ms=offset_ms + int(segment.end_ms),
                    speaker_id=segment.speaker_id,
                    words=[
                        TranscriptWord(
                            text=word.text,
                            start_ms=offset_ms + int(word.start_ms),
                            end_ms=offset_ms + int(word.end_ms),
                        )
                        for word in segment.words
                    ],
                )
            )
        return adjusted

    @staticmethod
    def _build_srt(segments: list[TranscriptSegment]) -> str:
        lines: list[str] = []
        for index, segment in enumerate(segments, start=1):
            lines.append(str(index))
            lines.append(
                f"{AudioTranscriptionService._format_srt_ms(segment.start_ms)} --> "
                f"{AudioTranscriptionService._format_srt_ms(segment.end_ms)}"
            )
            lines.append(segment.text)
            lines.append("")
        return "\n".join(lines).strip()

    @staticmethod
    def _format_srt_ms(value: int) -> str:
        safe = max(0, int(value))
        total_seconds, milliseconds = divmod(safe, 1000)
        minutes, seconds = divmod(total_seconds, 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"

    @staticmethod
    def _emit_progress(callback: ProgressCallback | None, current: int, total: int, message: str) -> None:
        if callback is not None:
            callback(current, total, message)

    @staticmethod
    def _check_cancel(should_cancel: CancelCallback | None) -> None:
        if should_cancel is not None and should_cancel():
            raise AudioTranscriptionError("用户已取消语音转写。")

    @staticmethod
    def _sign(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()
