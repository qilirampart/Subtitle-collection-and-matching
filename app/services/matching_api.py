from __future__ import annotations

import csv
import io
import json
import time
from dataclasses import dataclass

import requests


class MatchingApiError(RuntimeError):
    pass


DEFAULT_MATCHING_SERVICE_URL = "http://novel-similarity-dev.dzkjm.cn"


def _item_decision(item: dict[str, object]) -> dict[str, object]:
    raw_payload = item.get("result_payload_json")
    payload: dict[str, object] = {}
    if isinstance(raw_payload, dict):
        payload = raw_payload
    elif raw_payload:
        try:
            parsed = json.loads(str(raw_payload))
            if isinstance(parsed, dict):
                payload = parsed
        except (TypeError, ValueError):
            payload = {}
    decision = payload.get("decision")
    if isinstance(decision, dict) and decision.get("status"):
        return decision
    # Keep old task results readable; new results always carry decision.
    if item.get("matched_book_id"):
        return {
            "matched": True,
            "status": "matched",
            "reason": "legacy_matched_result",
            "book_id": str(item.get("matched_book_id") or ""),
            "book_name": str(item.get("matched_book_name") or ""),
            "matched_episode_order": item.get("matched_episode_order"),
        }
    return {
        "matched": False,
        "status": "not_matched",
        "reason": "legacy_result_without_final_decision",
    }


def aggregate_video_results(detail: dict[str, object]) -> list[dict[str, object]]:
    """Collapse segment-level task items into video-level review summaries."""
    raw_items = detail.get("items")
    items = raw_items if isinstance(raw_items, list) else []
    grouped: dict[str, dict[str, object]] = {}
    for raw_item in items:
        if not isinstance(raw_item, dict):
            continue
        video_id = str(raw_item.get("source_video_id") or raw_item.get("source_ref") or "unknown")
        group = grouped.setdefault(
            video_id,
            {
                "video_id": video_id,
                "title": str(raw_item.get("source_display_title") or ""),
                "channel": str(raw_item.get("source_channel") or ""),
                "segment_count": 0,
                "matched_segment_count": 0,
                "review_segment_count": 0,
                "not_matched_segment_count": 0,
                "matched_candidates": {},
                "review_items": [],
            },
        )
        group["segment_count"] = int(group["segment_count"]) + 1
        decision = _item_decision(raw_item)
        status = str(decision.get("status") or "not_matched")
        if status == "matched":
            group["matched_segment_count"] = int(group["matched_segment_count"]) + 1
        elif status == "review_required":
            group["review_segment_count"] = int(group["review_segment_count"]) + 1
        else:
            group["not_matched_segment_count"] = int(group["not_matched_segment_count"]) + 1

        if status == "review_required":
            review_items = group["review_items"]
            if isinstance(review_items, list):
                review_items.append(
                    {
                        "item_order": raw_item.get("item_order"),
                        "segment_order": raw_item.get("source_segment_order"),
                        "reason": str(decision.get("reason") or ""),
                        "book_id": str(decision.get("book_id") or ""),
                        "book_name": str(decision.get("book_name") or ""),
                        "episode_order": decision.get("matched_episode_order"),
                        "youtube_time_start": str(raw_item.get("source_time_start") or ""),
                        "youtube_time_end": str(raw_item.get("source_time_end") or ""),
                    }
                )
            continue

        if status != "matched":
            continue
        book_id = str(decision.get("book_id") or raw_item.get("matched_book_id") or "")
        if not book_id:
            continue
        candidates = group["matched_candidates"]
        if not isinstance(candidates, dict):
            continue
        candidate = candidates.setdefault(
            book_id,
            {
                "book_id": book_id,
                "book_name": str(decision.get("book_name") or raw_item.get("matched_book_name") or ""),
                "matched_episode_orders": [],
                "segment_hits": 0,
                "best_lexical_score": None,
                "time_ranges": [],
            },
        )
        candidate["segment_hits"] = int(candidate["segment_hits"]) + 1
        episode_order = decision.get("matched_episode_order") or raw_item.get("matched_episode_order")
        if episode_order is not None and episode_order not in candidate["matched_episode_orders"]:
            candidate["matched_episode_orders"].append(episode_order)
        score = raw_item.get("lexical_score")
        if isinstance(score, (int, float)):
            current = candidate.get("best_lexical_score")
            if current is None or float(score) > float(current):
                candidate["best_lexical_score"] = float(score)
        start = str(raw_item.get("source_time_start") or "")
        end = str(raw_item.get("source_time_end") or "")
        if start or end:
            candidate["time_ranges"].append({"start": start, "end": end})

    result: list[dict[str, object]] = []
    for group in grouped.values():
        candidates = group.pop("matched_candidates")
        ordered = sorted(
            (value for value in candidates.values() if isinstance(value, dict)),
            key=lambda value: (-int(value.get("segment_hits") or 0), -float(value.get("best_lexical_score") or float("-inf"))),
        )
        group["matched_candidates"] = ordered
        review_items = group.get("review_items")
        if isinstance(review_items, list):
            group["review_items"] = sorted(review_items, key=lambda value: int(value.get("segment_order") or 0))
        result.append(group)
    return result


def build_matching_result_rows(
    detail: dict[str, object],
    source_items: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Build one review-friendly row per source video from segment-level results."""

    def source_key(item: dict[str, object]) -> str:
        return str(item.get("source_video_id") or item.get("source_ref") or "unknown").strip() or "unknown"

    def append_unique(values: list[str], value: object) -> None:
        text = str(value or "").strip()
        if text and text not in values:
            values.append(text)

    def payload_of(item: dict[str, object]) -> dict[str, object]:
        raw_payload = item.get("result_payload_json")
        if isinstance(raw_payload, dict):
            return raw_payload
        try:
            parsed = json.loads(str(raw_payload or "{}"))
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def candidate_for_book(candidates: list[dict[str, object]], book_id: str) -> dict[str, object]:
        return next(
            (candidate for candidate in candidates if str(candidate.get("book_id") or "") == book_id),
            {},
        )

    def candidate_evidence(candidate: dict[str, object], fallback: dict[str, object]) -> tuple[str, str]:
        evidence = candidate.get("evidence") if isinstance(candidate.get("evidence"), dict) else {}
        fallback_evidence = fallback.get("evidence") if isinstance(fallback.get("evidence"), dict) else {}
        window_uid = str(
            evidence.get("window_uid")
            or evidence.get("evidence_window_uid")
            or candidate.get("evidence_window_uid")
            or candidate.get("window_uid")
            or fallback_evidence.get("window_uid")
            or fallback.get("evidence_window_uid")
            or ""
        ).strip()
        window_text = str(
            evidence.get("window_text")
            or evidence.get("window_text_preview")
            or candidate.get("window_text")
            or fallback_evidence.get("window_text")
            or fallback_evidence.get("window_text_preview")
            or ""
        ).strip()
        return window_uid, window_text

    def candidate_terms(candidate: dict[str, object]) -> list[str]:
        metrics = candidate.get("match_metrics") if isinstance(candidate.get("match_metrics"), dict) else {}
        values = metrics.get("shared_trigrams") if isinstance(metrics.get("shared_trigrams"), list) else []
        return [str(value).strip() for value in values if str(value).strip()][:80]

    def candidate_summary(candidate: dict[str, object], fallback: dict[str, object]) -> dict[str, object]:
        # Strong candidates have the business-facing coverage values, while the
        # raw candidate commonly carries the evidence window and shared phrases.
        source = candidate if candidate else fallback
        return {
            "review_priority": source.get("review_priority") or source.get("rank") or fallback.get("rank") or "",
            "book_id": str(source.get("book_id") or fallback.get("book_id") or "").strip(),
            "book_name": str(source.get("book_name") or fallback.get("book_name") or "").strip(),
            "episode_order": source.get("matched_episode_order") or source.get("episode_order") or fallback.get("episode_order") or "",
            "aggregate_text_coverage_rate": source.get("aggregate_text_coverage_rate"),
            "text_coverage_rate": source.get("text_coverage_rate"),
            "matched_window_count": source.get("matched_window_count"),
            "evidence_coverage_rate": source.get("evidence_coverage_rate"),
            "semantic_score": source.get("semantic_score"),
        }

    def rate_text(value: object) -> str:
        if isinstance(value, (int, float)):
            return f"{float(value) * 100:.2f}%"
        return "历史任务未计算"

    sources: dict[str, dict[str, str]] = {}
    source_segments: dict[tuple[str, str], str] = {}
    for item in source_items:
        if not isinstance(item, dict):
            continue
        key = source_key(item)
        subtitle = str(
            item.get("source_caption_text")
            or item.get("source_text_original")
            or item.get("query_text")
            or ""
        ).strip()
        current = sources.setdefault(
            key,
            {"source_channel": "", "source_title": "", "source_subtitle": "", "source_url": ""},
        )
        current["source_channel"] = current["source_channel"] or str(item.get("source_channel") or "").strip()
        current["source_title"] = current["source_title"] or str(item.get("source_display_title") or "").strip()
        source_ref = str(item.get("source_ref") or "").strip()
        source_description = str(item.get("source_description") or "").strip()
        source_url = source_ref if source_ref.startswith(("http://", "https://")) else source_description or source_ref
        current["source_url"] = current["source_url"] or source_url
        if len(subtitle) > len(current["source_subtitle"]):
            current["source_subtitle"] = subtitle
        segment_order = str(item.get("source_segment_order") or "").strip()
        segment_text = str(item.get("source_text_original") or item.get("query_text") or subtitle).strip()
        if segment_order and segment_text:
            source_segments[(key, segment_order)] = segment_text

    grouped: dict[str, dict[str, object]] = {}
    raw_items = detail.get("items")
    for item in raw_items if isinstance(raw_items, list) else []:
        if not isinstance(item, dict):
            continue
        key = source_key(item)
        group = grouped.setdefault(
            key,
            {
                "matched_count": 0,
                "review_count": 0,
                "not_matched_count": 0,
                "book_names": [],
                "book_ids": [],
                "episode_orders": [],
                "time_ranges": [],
                "reasons": [],
                "outcomes": [],
                "user_messages": [],
                "strong_candidates": [],
                "evidence_pairs": [],
                "execution": {},
                "translation_fallback": {},
                "translated_query_text": "",
                "review_feedback": {},
            },
        )
        decision = _item_decision(item)
        payload = payload_of(item)
        execution = payload.get("execution") if isinstance(payload.get("execution"), dict) else {}
        if execution and not group.get("execution"):
            group["execution"] = execution
        translation_fallback = payload.get("translation_fallback") if isinstance(payload.get("translation_fallback"), dict) else {}
        if translation_fallback:
            group["translation_fallback"] = translation_fallback
        translated_query_text = str(payload.get("translated_query_text") or "").strip()
        if translated_query_text and not group.get("translated_query_text"):
            group["translated_query_text"] = translated_query_text
        review_feedback = payload.get("review_feedback") if isinstance(payload.get("review_feedback"), dict) else {}
        if review_feedback and not group.get("review_feedback"):
            group["review_feedback"] = review_feedback
        status = str(decision.get("status") or "not_matched").strip().lower()
        outcome = str(decision.get("outcome") or "").strip().lower()
        if not outcome:
            outcome = "confirmed_match" if status == "matched" else "potential_match" if status == "review_required" else "no_match"
        append_unique(group["outcomes"], outcome)
        append_unique(group["user_messages"], decision.get("user_message"))
        if status == "matched":
            group["matched_count"] = int(group["matched_count"]) + 1
        elif status == "review_required":
            group["review_count"] = int(group["review_count"]) + 1
        else:
            group["not_matched_count"] = int(group["not_matched_count"]) + 1
        if status in {"matched", "review_required"}:
            append_unique(group["reasons"], decision.get("user_message") or decision.get("reason"))
            start = str(item.get("source_time_start") or "").strip()
            end = str(item.get("source_time_end") or "").strip()
            append_unique(group["time_ranges"], f"{start}-{end}".strip("-"))

            raw_candidate_values = (
                payload.get("translation_candidates", [])
                if outcome == "translation_assisted_match"
                else payload.get("candidates", [])
            )
            raw_candidates = [value for value in raw_candidate_values if isinstance(value, dict)]
            decision_rank = decision.get("candidate_rank")
            decision_book_id = str(decision.get("book_id") or "").strip()
            selected_candidate = next(
                (
                    value for value in raw_candidates
                    if isinstance(value, dict) and (
                        value.get("rank") == decision_rank
                        or (decision_book_id and str(value.get("book_id") or "") == decision_book_id)
                    )
                ),
                next((value for value in raw_candidates if isinstance(value, dict)), {}),
            )
            strong = decision.get("strong_match_candidates")
            strong_candidates = [value for value in strong if isinstance(value, dict)] if isinstance(strong, list) else []
            candidate_specs = strong_candidates if outcome == "content_matched_ambiguous" and strong_candidates else [selected_candidate or decision]
            segment_order = str(item.get("source_segment_order") or "").strip()
            source_text = source_segments.get((key, segment_order)) or str(item.get("query_text") or "").strip()
            for specification in candidate_specs:
                book_id = str(specification.get("book_id") or decision_book_id or "").strip()
                candidate = candidate_for_book(raw_candidates, book_id) or selected_candidate or specification
                summary = candidate_summary(specification, candidate)
                append_unique(group["book_names"], summary["book_name"])
                append_unique(group["book_ids"], summary["book_id"])
                append_unique(group["episode_orders"], summary["episode_order"])
                candidate_key = f"{summary['book_id']}:{summary['episode_order']}"
                summaries = group["strong_candidates"]
                if isinstance(summaries, list) and not any(
                    isinstance(value, dict) and f"{value.get('book_id')}:{value.get('episode_order')}" == candidate_key
                    for value in summaries
                ):
                    summaries.append(summary)

                window_uid, matched_text = candidate_evidence(candidate, specification)
                if not (window_uid or matched_text or summary["book_id"]):
                    continue
                pair = {
                    "segment_order": segment_order,
                    "source_text": source_text,
                    "matched_text": matched_text,
                    "window_uid": window_uid,
                    "book_name": summary["book_name"],
                    "book_id": summary["book_id"],
                    "episode_order": str(summary["episode_order"] or "").strip(),
                    "reason": str(decision.get("user_message") or decision.get("reason") or "").strip(),
                    "outcome": outcome,
                    "coverage_text": rate_text(summary["aggregate_text_coverage_rate"]),
                    "single_window_coverage_text": rate_text(summary["text_coverage_rate"]),
                    "matched_window_count": summary["matched_window_count"],
                    "evidence_coverage_rate": summary["evidence_coverage_rate"],
                    "semantic_score": summary["semantic_score"],
                    "review_priority": summary["review_priority"],
                    "execution": execution,
                    "highlight_terms": candidate_terms(candidate),
                }
                evidence_pairs = group["evidence_pairs"]
                pair_key = f"{window_uid}:{summary['book_id']}:{segment_order}"
                if isinstance(evidence_pairs, list) and not any(
                    isinstance(value, dict) and str(value.get("pair_key") or "") == pair_key
                    for value in evidence_pairs
                ):
                    pair["pair_key"] = pair_key
                    evidence_pairs.append(pair)

    rows: list[dict[str, object]] = []
    for key in dict.fromkeys([*sources, *grouped]):
        source = sources.get(key, {})
        group = grouped.get(key, {})
        matched_count = int(group.get("matched_count") or 0)
        review_count = int(group.get("review_count") or 0)
        not_matched_count = int(group.get("not_matched_count") or 0)
        outcomes = set(group.get("outcomes") or [])
        if "confirmed_match" in outcomes or matched_count:
            outcome = "confirmed_match"
        elif "content_matched_ambiguous" in outcomes:
            outcome = "content_matched_ambiguous"
        elif "translation_assisted_match" in outcomes:
            outcome = "translation_assisted_match"
        elif "potential_match" in outcomes or review_count:
            outcome = "potential_match"
        else:
            outcome = "no_match"
        rows.append(
            {
                "source_video_id": key,
                "source_channel": str(source.get("source_channel") or ""),
                "source_title": str(source.get("source_title") or ""),
                "source_subtitle": str(source.get("source_subtitle") or ""),
                "source_url": str(source.get("source_url") or ""),
                "match_status": outcome,
                "outcome": outcome,
                "user_message": " | ".join(group.get("user_messages") or []),
                "matched_book_names": " | ".join(group.get("book_names") or []),
                "matched_book_ids": " | ".join(group.get("book_ids") or []),
                "matched_episode_orders": " | ".join(group.get("episode_orders") or []),
                "matched_time_ranges": " | ".join(group.get("time_ranges") or []),
                "match_reasons": " | ".join(group.get("reasons") or []),
                "matched_segment_count": matched_count,
                "review_segment_count": review_count,
                "not_matched_segment_count": not_matched_count,
                "strong_candidates": list(group.get("strong_candidates") or []),
                "evidence_pairs": list(group.get("evidence_pairs") or []),
                "execution": dict(group.get("execution") or {}),
                "translation_fallback": dict(group.get("translation_fallback") or {}),
                "translated_query_text": str(group.get("translated_query_text") or ""),
                "review_feedback": dict(group.get("review_feedback") or {}),
            }
        )
    return rows


@dataclass(frozen=True)
class MatchingServiceConfig:
    base_url: str = DEFAULT_MATCHING_SERVICE_URL
    timeout_seconds: int = 45


class DramaSubtitleMatchingClient:
    """Authenticated client for the central Novel Similarity Service subtitle API."""

    def __init__(self, config: MatchingServiceConfig) -> None:
        self._base_url = config.base_url.rstrip("/")
        self._timeout_seconds = max(5, int(config.timeout_seconds or 45))
        self._session = requests.Session()

    def login(self, username: str, password: str) -> dict[str, object]:
        return self._request("POST", "/api/v1/auth/login", json={"username": username, "password": password})

    def current_user(self) -> dict[str, object]:
        return self._request("GET", "/api/v1/auth/me")

    def compare(
        self,
        query_text: str,
        *,
        language_code: str = "",
        translation_fallback: bool = True,
        top_k: int = 10,
        window_limit: int = 200,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "query_text": str(query_text or "").strip(),
            "top_k": max(1, min(int(top_k or 10), 20)),
            "window_limit": max(1, min(int(window_limit or 200), 500)),
            "translation_fallback": bool(translation_fallback),
        }
        if language_code:
            payload["language_code"] = language_code
        if not payload["query_text"]:
            raise MatchingApiError("Subtitle text is empty.")
        return self._request("POST", "/api/v1/drama-subtitles/compare", json=payload)

    def video_compare(
        self,
        query_text: str,
        *,
        cues: list[dict[str, object]],
        language_code: str = "",
        translation_fallback: bool = True,
        top_k: int = 10,
        window_limit: int = 200,
        semantic_enabled: bool = True,
        semantic_window_limit: int = 100,
    ) -> dict[str, object]:
        text = str(query_text or "").strip()
        normalized_cues = [
            {
                "start_seconds": float(cue.get("start_seconds") or 0),
                "end_seconds": float(cue.get("end_seconds") or cue.get("start_seconds") or 0),
                "text": str(cue.get("text") or "").strip(),
            }
            for cue in cues
            if isinstance(cue, dict) and str(cue.get("text") or "").strip()
        ]
        if not text:
            raise MatchingApiError("Subtitle text is empty.")
        if not normalized_cues:
            normalized_cues = [{"start_seconds": 0.0, "end_seconds": 0.0, "text": text}]
        payload: dict[str, object] = {
            "query_text": text,
            "cues": normalized_cues,
            "top_k": max(1, min(int(top_k or 10), 20)),
            "window_limit": max(1, min(int(window_limit or 200), 500)),
            "semantic_enabled": bool(semantic_enabled),
            "semantic_window_limit": max(1, min(int(semantic_window_limit or 100), 500)),
            "translation_fallback": bool(translation_fallback),
        }
        if language_code:
            payload["language_code"] = language_code
        return self._request("POST", "/api/v1/drama-subtitles/video-compare", json=payload)

    def task_detail(self, task_id: str) -> dict[str, object]:
        return self._request("GET", f"/api/v1/drama-subtitles/tasks/{task_id}")

    def evidence_context(
        self,
        window_uid: str,
        *,
        context_chars: int = 2400,
        before_lines: int = 6,
    ) -> dict[str, object]:
        uid = str(window_uid or "").strip()
        if not uid:
            raise MatchingApiError("Evidence window ID is empty.")
        return self._request(
            "GET",
            f"/api/v1/drama-subtitles/evidence-context/{uid}",
            params={
                "context_chars": max(600, min(int(context_chars or 2400), 5000)),
                "before_lines": max(0, min(int(before_lines or 6), 30)),
            },
        )

    def submit_batch(
        self,
        items: list[dict[str, object]],
        *,
        top_k: int = 10,
        window_limit: int = 200,
        semantic_enabled: bool = True,
        semantic_window_limit: int = 100,
    ) -> dict[str, object]:
        if not items:
            raise MatchingApiError("No subtitle items were supplied.")
        buffer = io.StringIO(newline="")
        fieldnames = [
            "source_ref",
            "query_text",
            "source_platform",
            "source_display_title",
            "source_description",
            "source_video_id",
            "source_channel",
            "source_upload_date",
            "source_caption_language",
            "source_caption_source",
            "source_segment_order",
            "source_time_start",
            "source_time_end",
            "source_text_original",
        ]
        writer = csv.DictWriter(buffer, fieldnames=fieldnames)
        writer.writeheader()
        for index, item in enumerate(items, start=1):
            writer.writerow(
                {
                    "source_ref": str(item.get("source_ref") or f"item_{index}"),
                    "query_text": str(item.get("query_text") or ""),
                    "source_platform": str(item.get("source_platform") or "YouTube"),
                    "source_display_title": str(item.get("source_display_title") or ""),
                    "source_description": str(item.get("source_description") or ""),
                    "source_video_id": str(item.get("source_video_id") or ""),
                    "source_channel": str(item.get("source_channel") or ""),
                    "source_upload_date": str(item.get("source_upload_date") or ""),
                    "source_caption_language": str(item.get("source_caption_language") or ""),
                    "source_caption_source": str(item.get("source_caption_source") or ""),
                    "source_segment_order": str(item.get("source_segment_order") or ""),
                    "source_time_start": str(item.get("source_time_start") or ""),
                    "source_time_end": str(item.get("source_time_end") or ""),
                    "source_text_original": str(item.get("source_text_original") or item.get("query_text") or ""),
                }
            )
        return self._request(
            "POST",
            "/api/v1/drama-subtitles/tasks",
            files={"file": ("youtube_subtitles.csv", buffer.getvalue().encode("utf-8-sig"), "text/csv")},
            data={
                "top_k": max(1, min(int(top_k or 10), 20)),
                "window_limit": max(1, min(int(window_limit or 200), 500)),
                "semantic_enabled": "true" if semantic_enabled else "false",
                "semantic_window_limit": max(1, min(int(semantic_window_limit or 100), 500)),
            },
        )

    def pause_task(self, task_id: str) -> dict[str, object]:
        return self._request("POST", f"/api/v1/drama-subtitles/tasks/{task_id}/pause")

    def resume_task(self, task_id: str) -> dict[str, object]:
        return self._request("POST", f"/api/v1/drama-subtitles/tasks/{task_id}/resume")

    def cancel_task(self, task_id: str) -> dict[str, object]:
        return self._request("POST", f"/api/v1/drama-subtitles/tasks/{task_id}/cancel")

    def delete_task(self, task_id: str) -> dict[str, object]:
        return self._request("DELETE", f"/api/v1/drama-subtitles/tasks/{task_id}")

    def wait_for_task(
        self,
        task_id: str,
        *,
        poll_seconds: float = 2.0,
        timeout_seconds: int = 3600,
        progress_callback=None,
    ) -> dict[str, object]:
        deadline = time.monotonic() + max(1, int(timeout_seconds or 3600))
        while True:
            detail = self.task_detail(task_id)
            task = detail.get("task") if isinstance(detail.get("task"), dict) else {}
            status = str(task.get("status") or "").lower()
            if progress_callback is not None:
                progress_callback(detail)
            if status in {"completed", "partial_failed", "failed", "cancelled", "canceled", "paused"}:
                return detail
            if time.monotonic() >= deadline:
                raise MatchingApiError(f"Matching task timed out: {task_id}")
            time.sleep(max(0.2, float(poll_seconds or 2.0)))

    def _request(self, method: str, path: str, **kwargs) -> dict[str, object]:
        try:
            response = self._session.request(
                method,
                f"{self._base_url}{path}",
                timeout=self._timeout_seconds,
                **kwargs,
            )
        except requests.RequestException as exc:
            raise MatchingApiError(f"Matching service request failed: {exc}") from exc
        try:
            payload = response.json()
        except ValueError:
            payload = {"detail": response.text.strip()}
        if not response.ok:
            detail = payload.get("detail") if isinstance(payload, dict) else str(payload)
            raise MatchingApiError(f"Matching service returned HTTP {response.status_code}: {detail}")
        return payload if isinstance(payload, dict) else {}
