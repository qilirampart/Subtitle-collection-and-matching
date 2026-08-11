from __future__ import annotations

import base64
import subprocess
from pathlib import Path

from PySide6.QtCore import QTimer, QUrl, Qt, Signal
from PySide6.QtWidgets import QDialog, QGridLayout, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from app.config.settings import EXTRACTED_AUDIO_DIR, FFMPEG_EXECUTABLE_PATH
from app.utils.ffmpeg import _hidden_process_kwargs
from app.utils.logger import get_logger
from app.utils.paths import next_compact_name
from app.ui.window_geometry import apply_responsive_window_geometry


class BrowserAudioCaptureDialog(QDialog):
    """Captures decoded YouTube audio with a small pool of Chromium pages."""

    completed = Signal(object, object)  # audio paths by video id, failed ids

    def __init__(self, items: list[dict[str, object]], seconds: int, concurrency: int = 1, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("浏览器高速音频采集")
        # Use a real top-level window so it cannot be covered by the main window while WebEngine loads.
        self.setWindowFlag(Qt.WindowType.Window, True)
        self.setWindowFlag(Qt.WindowType.WindowMinimizeButtonHint, True)
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, True)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        apply_responsive_window_geometry(
            self,
            preferred_width=1000,
            preferred_height=680,
            minimum_width=760,
            minimum_height=480,
        )
        self._logger = get_logger(__name__)
        self._items = items
        self._seconds = max(1, int(seconds))
        self._concurrency = max(1, min(int(concurrency or 1), 3))
        self._next_index = 0
        self._finished = 0
        self._paths: dict[str, str] = {}
        self._failed: list[str] = []
        self._failure_reasons: dict[str, str] = {}
        self._active: dict[object, dict[str, object]] = {}
        self._cancelled = False
        self._started = False

        self._status = QLabel()
        self._grid = QGridLayout()
        root = QVBoxLayout(self)
        root.addWidget(self._status)
        root.addLayout(self._grid, 1)
        self._failure_detail = QLabel()
        self._failure_detail.setWordWrap(True)
        self._failure_detail.setStyleSheet("color: #9b2c2c;")
        self._failure_detail.hide()
        root.addWidget(self._failure_detail)
        self._fallback_button = QPushButton("使用稳定下载继续")
        self._fallback_button.clicked.connect(self._continue_with_stable_download)
        self._fallback_button.hide()
        action_row = QHBoxLayout()
        action_row.addStretch(1)
        action_row.addWidget(self._fallback_button)
        root.addLayout(action_row)
        self._pages = [self._create_browser(index) for index in range(self._concurrency)]
        for index, browser in enumerate(self._pages):
            self._grid.addWidget(browser, index // 2, index % 2)
            browser.loadFinished.connect(lambda ok, current=browser: self._on_loaded(current, ok))
        self._status.setText("浏览器窗口已打开，正在准备采集页面…")
        # Wait until Qt has painted the dialog before pages start loading. This prevents a flash-and-hide effect.
        QTimer.singleShot(150, self._start_capture)

    def _start_capture(self) -> None:
        if self._started or self._cancelled:
            return
        self._started = True
        self._logger.info("Browser audio capture window started. items=%s concurrency=%s", len(self._items), self._concurrency)
        for browser in self._pages:
            self._start_next(current=browser)

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        self._logger.info("Browser audio capture window shown.")

    def hideEvent(self, event) -> None:  # type: ignore[override]
        self._logger.info(
            "Browser audio capture window hidden. started=%s finished=%s total=%s cancelled=%s",
            self._started,
            self._finished,
            len(self._items),
            self._cancelled,
        )
        super().hideEvent(event)

    def _create_browser(self, _index: int):
        from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
        from PySide6.QtWebEngineWidgets import QWebEngineView

        if not hasattr(self, "_profile"):
            profile_dir = Path(__file__).resolve().parents[2] / "runtime" / "youtube_browser_profile"
            self._profile = QWebEngineProfile("youtube-capture", self)
            self._profile.setPersistentStoragePath(str(profile_dir))
            self._profile.setCachePath(str(profile_dir / "cache"))
        browser = QWebEngineView(self)
        page = QWebEnginePage(self._profile, self)
        page.settings().setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False)
        browser.setPage(page)
        return browser

    def _start_next(self, *, current) -> None:
        if self._next_index >= len(self._items):
            self._finish_if_complete()
            return
        item = self._items[self._next_index]
        self._next_index += 1
        self._active[current] = item
        video = item.get("video") or {}
        self._update_status()
        current.setUrl(QUrl(str(video.get("source_url") or "")))

    def _on_loaded(self, browser, ok: bool) -> None:
        if self._cancelled or browser not in self._active:
            return
        if not ok:
            self._finish_item(browser, "", "浏览器页面加载失败，未能打开视频页面。")
            return
        script = f"""
        (async () => {{
          let v = null;
          for (let i = 0; i < 100; i++) {{ v = document.querySelector('video'); if (v) break; await new Promise(r => setTimeout(r, 200)); }}
          if (!v || !v.captureStream) return {{error: 'video capture is unavailable'}};
          v.pause(); v.currentTime = 0; v.preservesPitch = true; v.playbackRate = 8;
          await v.play();
          const audio = new MediaStream(v.captureStream().getAudioTracks());
          if (!audio.getAudioTracks().length) return {{error: 'audio track is unavailable'}};
          const recorder = new MediaRecorder(audio, {{mimeType: 'audio/webm;codecs=opus'}});
          const chunks = []; recorder.ondataavailable = e => {{ if (e.data.size) chunks.push(e.data); }};
          const stopped = new Promise(resolve => recorder.onstop = resolve);
          const start = v.currentTime; recorder.start();
          await new Promise(resolve => {{ const poll = () => v.currentTime >= start + {self._seconds} || v.ended ? resolve() : setTimeout(poll, 100); poll(); }});
          recorder.stop(); await stopped; v.pause();
          const data = await new Promise(resolve => {{ const reader = new FileReader(); reader.onload = () => resolve(reader.result.split(',')[1]); reader.readAsDataURL(new Blob(chunks, {{type: 'audio/webm'}})); }});
          return {{data}};
        }})()
        """
        browser.page().runJavaScript(script, lambda result, current=browser: self._on_capture_result(current, result))

    def _on_capture_result(self, browser, result) -> None:
        if self._cancelled:
            return
        output_path = ""
        failure_reason = ""
        try:
            data = result.get("data") if isinstance(result, dict) else ""
            if not data:
                reason = str(result.get("error") or "") if isinstance(result, dict) else ""
                raise RuntimeError(reason or "浏览器未返回音频数据")
            EXTRACTED_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
            captured = EXTRACTED_AUDIO_DIR / f"{next_compact_name('browser_capture')}.webm"
            captured.write_bytes(base64.b64decode(data))
            restored = EXTRACTED_AUDIO_DIR / f"{next_compact_name('browser_audio')}.wav"
            subprocess.run(
                [str(FFMPEG_EXECUTABLE_PATH), "-y", "-i", str(captured), "-filter:a", "atempo=0.5,atempo=0.5,atempo=0.5", str(restored)],
                capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
                **_hidden_process_kwargs(),
            )
            output_path = str(restored)
        except Exception as exc:  # noqa: BLE001
            output_path = ""
            failure_reason = str(exc)
            self._logger.warning("Browser audio capture failed: %s", failure_reason)
        self._finish_item(browser, output_path, failure_reason)

    def _finish_item(self, browser, output_path: str, failure_reason: str = "") -> None:
        item = self._active.pop(browser, {})
        video = item.get("video") or {}
        video_id = str(video.get("video_id") or "")
        if video_id and output_path:
            self._paths[video_id] = output_path
        elif video_id:
            self._failed.append(video_id)
            self._failure_reasons[video_id] = failure_reason or "未能从浏览器页面获得音频。"
        self._finished += 1
        self._update_status()
        self._start_next(current=browser)

    def _update_status(self) -> None:
        self._status.setText(
            f"浏览器高速采集：已完成 {self._finished}/{len(self._items)}，并发 {self._concurrency} 路"
        )

    def _finish_if_complete(self) -> None:
        if self._started and self._finished >= len(self._items) and not self._active:
            self._logger.info("Browser audio capture completed. captured=%s failed=%s", len(self._paths), len(self._failed))
            if not self._paths and self._failed:
                self._show_all_failed_state()
                return
            self.completed.emit(self._paths, self._failed)
            self.accept()

    def _show_all_failed_state(self) -> None:
        reasons = list(dict.fromkeys(self._failure_reasons.values()))
        reason_text = "；".join(reasons[:2]) or "未能从浏览器页面获得音频。"
        self._status.setText("浏览器高速采集未成功，尚未开始稳定下载。")
        self._failure_detail.setText(f"失败原因：{reason_text}\n可检查窗口内的视频页面，或点击下方按钮改用稳定下载方案。")
        self._failure_detail.show()
        self._fallback_button.show()
        self.raise_()
        self.activateWindow()

    def _continue_with_stable_download(self) -> None:
        self._logger.info("Browser capture fallback accepted by user. failed=%s", len(self._failed))
        self.completed.emit(self._paths, self._failed)
        self.accept()

    def reject(self) -> None:
        if self._cancelled:
            return super().reject()
        self._cancelled = True
        unresolved = list(self._active.values()) + self._items[self._next_index :]
        for item in unresolved:
            video = item.get("video") or {}
            video_id = str(video.get("video_id") or "")
            if video_id and video_id not in self._paths and video_id not in self._failed:
                self._failed.append(video_id)
        self.completed.emit(self._paths, self._failed)
        super().reject()
