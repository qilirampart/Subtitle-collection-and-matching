# YouTube Subtitle Verification Helper

This workspace is intentionally separate from the material analysis helper.

Scope:

- Collect public YouTube channel videos through yt-dlp.
- Retrieve public or automatic subtitle tracks, then retain the leading 1-3 minutes.
- Fall back to audio transcription only when captions are unavailable.
- Submit subtitle text to the Novel Similarity Service API and retain evidence results.

Out of scope:

- Douyin, Kuaishou, ShortFlix, clipping, keyframe extraction, video review, and material-package export.
- Packaging the subtitle index, novel data, or matching engine into the desktop application.

The desktop helper is an API client. The matching database and retrieval index remain in:

`E:\点众\小说库相似度比对服务工作区`

## Initial layout

- `app/services`: reusable collection, subtitle, matching API, and task-state services.
- `docs`: product boundary and implementation decisions.
- `runtime`: local cookies, ASR configuration, ffmpeg, audio cache, and recoverable task state. Do not commit credentials.

## Current integration target

- `POST /api/v1/auth/login`
- `POST /api/v1/drama-subtitles/compare`
- `POST /api/v1/drama-subtitles/tasks`
- `GET /api/v1/drama-subtitles/tasks/{task_id}`

Matching always starts with the original subtitle language. Translation is a later, low-result fallback owned by the matching service; Traditional Chinese must not be silently converted to Simplified Chinese by this client.

## Run

Install dependencies with `pip install -r requirements.txt`, then run `python main.py` to open the focused desktop workflow. The CLI commands remain available for development and troubleshooting: `collect`, `inspect`, and `compare`.

On Windows, operators should double-click `启动YouTube字幕核验助手.vbs` for a normal launch without a command window. The diagnostic `启动YouTube字幕核验助手.bat` switches to the project directory and keeps the console open when startup fails, so dependency or configuration errors are visible instead of appearing as a silent flash.

## ASR fallback

When a selected video has no usable human or automatic caption, the desktop workflow checks the migrated ASR configuration. If a provider is ready, it downloads only the configured leading audio range and sends it through the migrated multi-provider ASR chain with circuit breaking and failover. The `ASR 配置` button edits the same provider structure used by the original project, and can import the old `runtime/api_config.json`.

The bundled `runtime/ffmpeg` directory is required for audio extraction and is intentionally kept with this standalone project.

## YouTube login and download speed

The main window provides a non-modal `YouTube Login` button. When YouTube requires account verification, or when a public stream is unusually slow, open this window, sign in with the normal YouTube page, then click `Sync login state`.

The application stores the exported session only in `runtime/youtube_cookies.txt` and automatically uses it for later channel collection, caption acquisition, and leading-audio downloads. It never stores the account password. Use `Clear login state` from the same window when the shared session should be removed.

Login can improve stream availability and sometimes improves download speed, but it cannot guarantee browser-playback speed. The next validation step is to download the same leading-audio range before and after login, using the same proxy route, then retain the faster route as the default.
