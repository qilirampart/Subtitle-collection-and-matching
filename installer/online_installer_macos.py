from __future__ import annotations

import hashlib
import json
import os
import platform
import plistlib
import queue
import shlex
import shutil
import subprocess
import tempfile
import threading
import tkinter as tk
import urllib.request
from pathlib import Path
from tkinter import messagebox, ttk


RELEASE_DOWNLOAD_BASE_URL = (
    "https://github.com/qilirampart/"
    "Subtitle-collection-and-matching/releases/latest/download"
)
APP_NAME = "YouTube字幕核验助手"


class MacOnlineInstaller(tk.Tk):
    """Small macOS bootstrap installer for the architecture-specific app bundle."""

    def __init__(self) -> None:
        super().__init__()
        self.title(f"{APP_NAME} 安装器")
        self.resizable(False, False)
        self.geometry("640x310")
        self._events: queue.Queue[tuple[str, object]] = queue.Queue()
        self._installing = False
        self._architecture = self._detect_architecture()

        body = ttk.Frame(self, padding=22)
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text=APP_NAME, font=("PingFang SC", 16, "bold")).pack(anchor=tk.W)
        ttk.Label(
            body,
            text=(
                f"将自动下载适用于 {self._architecture_label()} 的完整组件，"
                "校验后安装到“应用程序”目录。"
            ),
            wraplength=580,
        ).pack(anchor=tk.W, pady=(8, 16))
        ttk.Label(
            body,
            text="安装过程中 macOS 会请求管理员确认，用于复制应用程序。",
            wraplength=580,
        ).pack(anchor=tk.W)

        self.status_text = tk.StringVar(value="准备安装。")
        self.progress_text = tk.StringVar(value="等待开始")
        ttk.Label(body, textvariable=self.status_text, wraplength=580).pack(anchor=tk.W, pady=(18, 6))
        self.progress = ttk.Progressbar(body, mode="determinate", maximum=100)
        self.progress.pack(fill=tk.X)
        ttk.Label(body, textvariable=self.progress_text).pack(anchor=tk.E, pady=(4, 0))

        actions = ttk.Frame(body)
        actions.pack(fill=tk.X, pady=(18, 0))
        self.install_button = ttk.Button(actions, text="开始安装", command=self._start_install)
        self.install_button.pack(side=tk.RIGHT)
        ttk.Button(actions, text="取消", command=self.destroy).pack(side=tk.RIGHT, padx=(0, 8))
        self.after(120, self._drain_events)

    @staticmethod
    def _detect_architecture() -> str:
        machine = platform.machine().lower()
        if machine in {"arm64", "aarch64"}:
            return "arm64"
        if machine in {"x86_64", "amd64"}:
            return "x64"
        raise RuntimeError(f"不支持的 Mac 芯片架构：{machine or 'unknown'}")

    def _architecture_label(self) -> str:
        return "Apple Silicon（M 系列）" if self._architecture == "arm64" else "Intel Mac"

    def _start_install(self) -> None:
        if self._installing:
            return
        self._installing = True
        self.install_button.configure(state=tk.DISABLED)
        threading.Thread(target=self._install, daemon=True).start()

    def _install(self) -> None:
        try:
            asset_name = f"YouTubeSubtitleVerifier-macOS-{self._architecture}.dmg"
            hash_name = f"{asset_name}.sha256"
            archive_url = f"{RELEASE_DOWNLOAD_BASE_URL}/{asset_name}"
            hash_url = f"{RELEASE_DOWNLOAD_BASE_URL}/{hash_name}"
            self._emit("status", "正在读取最新版本的完整性校验信息...")
            expected_hash = self._download_hash(hash_url)

            with tempfile.TemporaryDirectory(prefix="subtitle-verifier-macos-") as temp_dir:
                archive_path = Path(temp_dir) / asset_name
                self._download(archive_url, archive_path)
                self._emit("status", "正在校验下载文件...")
                if self._sha256(archive_path) != expected_hash:
                    raise RuntimeError("下载文件校验失败，请检查网络后重试。")
                self._emit("status", "正在准备安装应用程序...")
                app_bundle, mount_point = self._mount_application(archive_path)
                try:
                    self._install_to_applications(app_bundle)
                finally:
                    self._detach(mount_point)
            self._emit("complete", None)
        except Exception as exc:  # noqa: BLE001
            self._emit("error", str(exc))

    @staticmethod
    def _download_hash(url: str) -> str:
        request = urllib.request.Request(url, headers={"User-Agent": "SubtitleVerifierMacInstaller"})
        with urllib.request.urlopen(request, timeout=30) as response:
            value = response.read().decode("utf-8").strip().split(maxsplit=1)[0].lower()
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise RuntimeError("最新发布版本缺少有效的完整性校验文件。")
        return value

    def _download(self, url: str, target: Path) -> None:
        request = urllib.request.Request(url, headers={"User-Agent": "SubtitleVerifierMacInstaller"})
        with urllib.request.urlopen(request, timeout=60) as response, target.open("wb") as output:
            total = int(response.headers.get("Content-Length") or 0)
            received = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                received += len(chunk)
                if total:
                    percent = min(100, int(received * 100 / total))
                    label = f"正在下载组件：{received / 1_048_576:.0f} / {total / 1_048_576:.0f} MB"
                else:
                    percent = 0
                    label = f"正在下载组件：{received / 1_048_576:.0f} MB"
                self._emit("progress", (percent, label))

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _mount_application(archive_path: Path) -> tuple[Path, Path]:
        attached = subprocess.check_output(
            ["hdiutil", "attach", "-nobrowse", "-readonly", "-plist", str(archive_path)],
            stderr=subprocess.STDOUT,
        )
        payload = plistlib.loads(attached)
        entities = payload.get("system-entities") if isinstance(payload, dict) else []
        mount_point = next(
            (
                Path(str(entity.get("mount-point")))
                for entity in entities or []
                if isinstance(entity, dict) and entity.get("mount-point")
            ),
            None,
        )
        if mount_point is None:
            raise RuntimeError("无法挂载下载的安装包。")
        app_bundle = next((path for path in mount_point.glob("*.app") if path.is_dir()), None)
        if app_bundle is None:
            subprocess.run(["hdiutil", "detach", str(mount_point), "-quiet"], check=False)
            raise RuntimeError("安装包中没有找到应用程序。")
        return app_bundle, mount_point

    @staticmethod
    def _detach(mount_point: Path) -> None:
        subprocess.run(["hdiutil", "detach", str(mount_point), "-quiet"], check=False)

    @staticmethod
    def _install_to_applications(app_bundle: Path) -> None:
        target = Path("/Applications") / app_bundle.name
        shell_command = f"rm -rf {shlex.quote(str(target))}; ditto {shlex.quote(str(app_bundle))} {shlex.quote(str(target))}"
        apple_script = f"do shell script {json.dumps(shell_command)} with administrator privileges"
        completed = subprocess.run(
            ["osascript", "-e", apple_script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()
            raise RuntimeError(detail or "未完成安装，请在系统提示中确认管理员授权后重试。")

    def _emit(self, kind: str, value: object) -> None:
        self._events.put((kind, value))

    def _drain_events(self) -> None:
        try:
            while True:
                kind, value = self._events.get_nowait()
                if kind == "status":
                    self.status_text.set(str(value))
                elif kind == "progress":
                    percent, label = value  # type: ignore[misc]
                    self.progress.configure(value=percent)
                    self.progress_text.set(str(label))
                elif kind == "complete":
                    self.progress.configure(value=100)
                    self.progress_text.set("安装完成")
                    self.status_text.set("安装完成，已复制到“应用程序”目录。")
                    self._installing = False
                    self.install_button.configure(state=tk.NORMAL)
                    if messagebox.askyesno("安装完成", f"{APP_NAME} 已安装完成。是否立即启动？"):
                        subprocess.Popen(["open", "-a", APP_NAME])
                elif kind == "error":
                    self._installing = False
                    self.install_button.configure(state=tk.NORMAL)
                    self.status_text.set("安装失败，请检查网络或系统授权后重试。")
                    messagebox.showerror("安装失败", str(value))
        except queue.Empty:
            pass
        self.after(120, self._drain_events)


if __name__ == "__main__":
    MacOnlineInstaller().mainloop()
