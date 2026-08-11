from __future__ import annotations

import base64
import json
import mimetypes
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import requests

from app.models import YouTubeVideo
from app.services.api_config_service import ApiConfigService
from app.task_control import TaskControl


@dataclass(frozen=True)
class CoverReviewResult:
    video_id: str
    title: str
    cover_path: str
    overall_risk: str = "unknown"
    risk_tags: tuple[str, ...] = ()
    summary: str = ""
    evidence: str = ""
    confidence: float = 0.0
    model_response: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class YouTubeCoverReviewService:
    """Runs a conservative, cover-only visual compliance pre-screen."""

    _TIMEOUT_SECONDS = 120
    _MAX_CONCURRENCY = 3
    _RISK_TAGS = {
        "minor": "未成年人",
        "underage": "未成年人",
        "child": "未成年人",
        "pregnancy": "孕妇",
        "pregnant": "孕妇",
        "sexual": "色情或性暗示",
        "sexual_content": "色情或性暗示",
        "student": "学生或校园",
        "school": "学生或校园",
    }
    _SYSTEM_PROMPT = (
        "你是海外营销素材封面合规初筛助手。你只能根据当前这一张视频封面中明确可见的"
        "人物、服装、动作、场景和文字进行判断，不要推断视频正片内容，也不要仅凭年龄不确定"
        "的成年人外观判定为未成年人。请重点检查四类风险：未成年人、孕妇、色情或明显性暗示、"
        "学生或校园元素。学生或校园元素本身不一定违规，但需要单独标记供人工复核。\n\n"
        "请严格只返回一个 JSON 对象，不要 Markdown，不要额外解释：\n"
        "{\n"
        '  "overall_risk": "safe|review|risk|unknown",\n'
        '  "risk_tags": ["未成年人", "孕妇", "色情或性暗示", "学生或校园"],\n'
        '  "summary": "不超过80字的结论",\n'
        '  "evidence": "说明封面中支持结论的可见证据；没有证据时写未发现明确证据",\n'
        '  "confidence": 0.0\n'
        "}\n"
        "规则：只看到疑似风险但无法确认时使用 review，不要强行判定 risk；"
        "明确出现风险内容才使用 risk；overall_risk 为 safe 时 risk_tags 必须为空数组；"
        "confidence 必须是 0 到 1 之间的小数。"
    )

    def __init__(self, config_service: ApiConfigService | None = None) -> None:
        self.config_service = config_service or ApiConfigService()

    def _active_profile(self) -> dict[str, Any] | None:
        for profile in self.config_service.get_llm_profiles():
            if self.config_service.is_llm_profile_ready(profile):
                return profile
        return None

    @staticmethod
    def _endpoint(api_base: str) -> str:
        base = api_base.rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        return f"{base}/chat/completions" if base.endswith("/v1") else f"{base}/v1/chat/completions"

    @staticmethod
    def _image_data_url(path: Path) -> str:
        mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:{mime_type};base64,{encoded}"

    @classmethod
    def _parse_response(cls, raw_text: str) -> dict[str, Any]:
        text = str(raw_text or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("模型没有返回 JSON 检测结果")
        payload = json.loads(text[start : end + 1])
        if not isinstance(payload, dict):
            raise ValueError("模型返回的检测结果不是对象")
        return payload

    @classmethod
    def _normalize_result(
        cls,
        video: YouTubeVideo,
        cover_path: Path,
        payload: dict[str, Any],
        *,
        model_response: str = "",
    ) -> CoverReviewResult:
        raw_tags = payload.get("risk_tags")
        if not isinstance(raw_tags, list):
            raw_tags = []
        tags: list[str] = []
        for raw_tag in raw_tags:
            key = str(raw_tag or "").strip().lower().replace(" ", "_")
            label = cls._RISK_TAGS.get(key, str(raw_tag or "").strip())
            if label and label not in tags:
                tags.append(label)
        overall = str(payload.get("overall_risk") or "unknown").strip().lower()
        if overall not in {"safe", "review", "risk", "unknown"}:
            overall = "review" if tags else "unknown"
        try:
            confidence = max(0.0, min(1.0, float(payload.get("confidence") or 0)))
        except (TypeError, ValueError):
            confidence = 0.0
        return CoverReviewResult(
            video.video_id,
            video.title,
            str(cover_path),
            overall,
            tuple(tags),
            str(payload.get("summary") or "").strip(),
            str(payload.get("evidence") or "").strip(),
            confidence,
            str(model_response or "").strip(),
        )

    def review_cover(
        self,
        video: YouTubeVideo,
        cover_path: str | Path,
        *,
        profile: dict[str, Any] | None = None,
    ) -> CoverReviewResult:
        path = Path(cover_path)
        if not path.is_file():
            return CoverReviewResult(video.video_id, video.title, str(path), error="封面文件不存在")
        active_profile = profile if profile is not None else self._active_profile()
        if active_profile is None:
            return CoverReviewResult(video.video_id, video.title, str(path), error="没有可用的语言模型配置")

        payload = {
            "model": str(active_profile["model"]),
            "temperature": float(active_profile.get("temperature") or 0),
            "messages": [
                {"role": "system", "content": self._SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"请检测这张视频封面。视频标题仅作参考：{video.title}"},
                        {"type": "image_url", "image_url": {"url": self._image_data_url(path)}},
                    ],
                },
            ],
        }
        headers = {
            "Authorization": f"Bearer {active_profile['api_key']}",
            "Content-Type": "application/json",
        }
        try:
            response = requests.post(
                self._endpoint(str(active_profile["api_base"])),
                headers=headers,
                json=payload,
                timeout=self._TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            response_payload = response.json()
            content = response_payload["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(str(item.get("text") or "") for item in content if isinstance(item, dict))
            raw_content = str(content)
            return self._normalize_result(
                video,
                path,
                self._parse_response(raw_content),
                model_response=raw_content,
            )
        except (OSError, KeyError, IndexError, TypeError, ValueError, requests.RequestException) as exc:
            return CoverReviewResult(video.video_id, video.title, str(path), error=str(exc))

    def review_batch(
        self,
        videos: list[YouTubeVideo],
        cover_paths: dict[str, str],
        *,
        progress_callback=None,
        task_control: TaskControl | None = None,
    ) -> tuple[list[CoverReviewResult], bool]:
        profile = self._active_profile()
        results: list[CoverReviewResult] = []
        if profile is None:
            for index, video in enumerate(videos, start=1):
                result = CoverReviewResult(
                    video.video_id,
                    video.title,
                    str(cover_paths.get(video.video_id) or ""),
                    error="没有可用的语言模型配置",
                )
                results.append(result)
                if progress_callback is not None:
                    progress_callback(index, len(videos), video, result)
            return results, False
        for batch_start in range(0, len(videos), self._MAX_CONCURRENCY):
            if task_control is not None and not task_control.checkpoint():
                return results, True
            batch = videos[batch_start : batch_start + self._MAX_CONCURRENCY]
            batch_results: dict[str, CoverReviewResult] = {}
            with ThreadPoolExecutor(max_workers=self._MAX_CONCURRENCY) as executor:
                futures = {
                    executor.submit(
                        self.review_cover,
                        video,
                        str(cover_paths.get(video.video_id) or ""),
                        profile=profile,
                    ): video
                    for video in batch
                }
                for future in as_completed(futures):
                    video = futures[future]
                    result = future.result()
                    batch_results[video.video_id] = result
                    if progress_callback is not None:
                        progress_callback(
                            batch_start + len(batch_results),
                            len(videos),
                            video,
                            result,
                        )
            results.extend(batch_results[video.video_id] for video in batch)
        return results, False
