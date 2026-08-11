from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from app.config.settings import FFMPEG_DIR, RESOURCE_ROOT


class FFmpegError(RuntimeError):
    pass


def _hidden_process_kwargs() -> dict[str, object]:
    kwargs: dict[str, object] = {}
    if os.name != "nt":
        return kwargs

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if creationflags:
        kwargs["creationflags"] = creationflags

    startupinfo_factory = getattr(subprocess, "STARTUPINFO", None)
    if startupinfo_factory is not None:
        startupinfo = startupinfo_factory()
        startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
        startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
        kwargs["startupinfo"] = startupinfo
    return kwargs


def merge_av_streams(video_path: str | Path, audio_path: str | Path, output_path: str | Path) -> None:
    video = str(video_path)
    audio = str(audio_path)
    output = str(output_path)
    command = [
        _resolve_binary("ffmpeg"),
        "-y",
        "-loglevel",
        "error",
        "-i",
        video,
        "-i",
        audio,
        "-c",
        "copy",
        output,
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        **_hidden_process_kwargs(),
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise FFmpegError(detail or "ffmpeg 合并音视频失败。")


def probe_media_duration_ms(media_path: str | Path) -> int:
    command = [
        _resolve_binary("ffprobe"),
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(media_path),
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        **_hidden_process_kwargs(),
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise FFmpegError(detail or "ffprobe 获取媒体时长失败。")
    try:
        return int(float((completed.stdout or "0").strip()) * 1000)
    except ValueError as exc:
        raise FFmpegError("无法解析媒体时长。") from exc


def probe_video_fps(media_path: str | Path) -> float:
    command = [
        _resolve_binary("ffprobe"),
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=avg_frame_rate,r_frame_rate",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(media_path),
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        **_hidden_process_kwargs(),
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise FFmpegError(detail or "ffprobe could not read video fps.")

    for line in (completed.stdout or "").splitlines():
        value = line.strip()
        if not value or value in {"0/0", "N/A"}:
            continue
        if "/" in value:
            numerator_text, denominator_text = value.split("/", 1)
            try:
                numerator = float(numerator_text)
                denominator = float(denominator_text)
            except ValueError:
                continue
            if denominator > 0 and numerator > 0:
                return numerator / denominator
            continue
        try:
            fps = float(value)
        except ValueError:
            continue
        if fps > 0:
            return fps
    raise FFmpegError("ffprobe did not return a valid video fps.")


def ensure_video_has_decodable_frame(media_path: str | Path) -> None:
    command = [
        _resolve_binary("ffmpeg"),
        "-v",
        "error",
        "-i",
        str(media_path),
        "-map",
        "0:v:0",
        "-frames:v",
        "1",
        "-f",
        "null",
        "-",
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        **_hidden_process_kwargs(),
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise FFmpegError(detail or "ffmpeg could not decode a video frame from the downloaded file.")


def extract_audio_track(
    source_path: str | Path,
    output_path: str | Path,
    *,
    start_ms: int | None = None,
    duration_ms: int | None = None,
    sample_rate: int = 16000,
    channels: int = 1,
    bitrate: str = "24k",
) -> None:
    command = [
        _resolve_binary("ffmpeg"),
        "-y",
        "-loglevel",
        "error",
    ]
    if start_ms is not None and start_ms > 0:
        command.extend(["-ss", f"{start_ms / 1000:.3f}"])
    command.extend(["-i", str(source_path)])
    if duration_ms is not None and duration_ms > 0:
        command.extend(["-t", f"{duration_ms / 1000:.3f}"])
    command.extend(
        [
            "-vn",
            "-ac",
            str(channels),
            "-ar",
            str(sample_rate),
            "-b:a",
            bitrate,
            "-codec:a",
            "libmp3lame",
            str(output_path),
        ]
    )
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        **_hidden_process_kwargs(),
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise FFmpegError(detail or "ffmpeg 音频提取失败。")


def extract_video_clip(
    source_path: str | Path,
    output_path: str | Path,
    *,
    start_ms: int = 0,
    duration_ms: int | None = None,
) -> None:
    command = [
        _resolve_binary("ffmpeg"),
        "-y",
        "-loglevel",
        "error",
    ]
    if start_ms > 0:
        command.extend(["-ss", f"{start_ms / 1000:.3f}"])
    command.extend(["-i", str(source_path)])
    if duration_ms is not None and duration_ms > 0:
        command.extend(["-t", f"{duration_ms / 1000:.3f}"])
    command.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        **_hidden_process_kwargs(),
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise FFmpegError(detail or "ffmpeg 视频片段截取失败。")


def extract_review_video_segment(
    source_path: str | Path,
    output_path: str | Path,
    *,
    start_ms: int = 0,
    duration_ms: int | None = None,
    width: int = 360,
) -> None:
    command = [
        _resolve_binary("ffmpeg"),
        "-y",
        "-loglevel",
        "error",
    ]
    if start_ms > 0:
        command.extend(["-ss", f"{start_ms / 1000:.3f}"])
    command.extend(["-i", str(source_path)])
    if duration_ms is not None and duration_ms > 0:
        command.extend(["-t", f"{duration_ms / 1000:.3f}"])
    command.extend(
        [
            "-vf",
            f"scale={max(240, int(width))}:-2",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "30",
            "-c:a",
            "aac",
            "-b:a",
            "48k",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
    )
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        **_hidden_process_kwargs(),
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise FFmpegError(detail or "ffmpeg could not prepare review video segment.")


def extract_video_frame(
    source_path: str | Path,
    output_path: str | Path,
    *,
    timestamp_ms: int = 0,
) -> None:
    command = [
        _resolve_binary("ffmpeg"),
        "-y",
        "-loglevel",
        "error",
        "-ss",
        f"{max(0, timestamp_ms) / 1000:.3f}",
        "-i",
        str(source_path),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(output_path),
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        **_hidden_process_kwargs(),
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise FFmpegError(detail or "ffmpeg could not extract a video frame.")


def _resolve_binary(binary_name: str) -> str:
    executable_names = [f"{binary_name}.exe", binary_name]
    candidate_roots = [
        FFMPEG_DIR,
        RESOURCE_ROOT / "runtime" / "ffmpeg",
    ]
    for root in candidate_roots:
        for executable_name in executable_names:
            candidate = root / executable_name
            if candidate.exists():
                return str(candidate)

    system_binary = shutil.which(binary_name)
    if system_binary:
        return system_binary
    raise FFmpegError(
        "未找到 ffmpeg/ffprobe，请把可执行文件放到 runtime/ffmpeg/ 目录，或加入系统 PATH。"
    )
