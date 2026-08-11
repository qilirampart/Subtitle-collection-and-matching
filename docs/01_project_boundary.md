# Project Boundary

## Objective

Provide an operator-facing workflow for:

1. collecting a public YouTube channel;
2. selecting videos;
3. extracting the first 60, 180, or 300 seconds of captions;
4. matching the selected subtitle text against the central drama subtitle index;
5. returning candidate title, source drama ID, episode, score, and subtitle evidence.

## Relationship to existing projects

The material analysis helper remains unchanged at:

`E:\点众\自动化工具\侵权巡检助手工作区\源码`

This project reuses its yt-dlp collection approach and its authenticated-browser cookie convention, but has its own runtime directory and release lifecycle.

The novel similarity service remains the only owner of the subtitle index and match result persistence at:

`E:\点众\小说库相似度比对服务工作区`

This desktop application must not copy the subtitle SQLite database, Qdrant collection, or novel data.

## Caption strategy

1. Prefer public human-created subtitles.
2. Use public automatic subtitles when no human-created subtitles are available.
3. Keep only the configured leading time range.
4. When no usable captions exist, use the migrated multi-provider ASR chain as a controlled audio-only fallback rather than downloading full videos by default.

YouTube Data API is not required. yt-dlp remains the channel, public-caption, and leading-audio acquisition method.

## Matching strategy

1. Preserve the original subtitle language and text. Only normalize whitespace and subtitle markup for retrieval input.
2. Submit the native-language query first. Traditional Chinese channels and overseas local dramas must not be translated before this first pass.
3. Keep returned evidence windows and scores with the YouTube video ID and source URL.
4. Add machine-translation fallback only when native-language results are absent or the best candidate is below a validated threshold.
5. Persist the original text, translated query, translation target language, and evidence language. Translation is recall material only, never the sole review evidence.

## Required service-side follow-up

The current matching API returns `book_id` and episode evidence. Before production release, confirm or add a stable mapping from `book_id` to the business-required source drama ID. The client must show both IDs when available.

The matching service now owns translation fallback and translation caching. The desktop client sends `translation_fallback: true`, preserves original subtitles, and treats `translation_assisted_match` only as a review-required result; it must not perform local translation or upgrade translated candidates to confirmed matches.
