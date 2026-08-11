from __future__ import annotations

import hashlib
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import urllib.request
import zipfile
from pathlib import Path
from tkinter import messagebox, ttk


RELEASE_DOWNLOAD_BASE_URL = (
    "https://github.com/qilirampart/"
    "Subtitle-collection-and-matching/releases/latest/download"
)
APP_DIRECTORY_NAME = "YouTube字幕核验助手"
APP_EXECUTABLE_NAME = "YouTube字幕核验助手.exe"
ARCHIVE_NAME = "YouTubeSubtitleVerifier-Windows-x64.zip"
HASH_NAME = f"{ARCHIVE_NAME}.sha256"


class OnlineInstaller(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("YouTube 字幕核验助手 安装器")
        self.resizable(False, False)
        self.geometry("620x300")
        self._events: queue.Queue[tuple[str, object]] = queue.Queue()
        self._installing = False

        default_root = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Dianzhong"
        self.install_path = tk.StringVar(value=str(default_root / APP_DIRECTORY_NAME))
        self.status_text = tk.StringVar(value="准备安装。安装器会自动下载并校验所需组件。")
        self.progress_text = tk.StringVar(value="等待开始")

        body = ttk.Frame(self, padding=22)
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text="YouTube 字幕核验助手", font=("Microsoft YaHei UI", 16, "bold")).pack(anchor=tk.W)
        ttk.Label(
            body,
            text="在线安装器会自动下载完整运行组件，无需手动配置 FFmpeg、Node 或浏览器依赖。",
            wraplength=560,
        ).pack(anchor=tk.W, pady=(8, 16))

        location = ttk.Frame(body)
        location.pack(fill=tk.X)
        ttk.Label(location, text="安装位置").pack(anchor=tk.W)
        row = ttk.Frame(location)
        row.pack(fill=tk.X, pady=(5, 0))
        ttk.Entry(row, textvariable=self.install_path).pack(side=tk.LEFT, fill=tk.X, expand=True)
        ttk.Button(row, text="选择...", command=self._choose_directory).pack(side=tk.LEFT, padx=(8, 0))

        ttk.Label(body, textvariable=self.status_text, wraplength=560).pack(anchor=tk.W, pady=(18, 6))
        self.progress = ttk.Progressbar(body, mode="determinate", maximum=100)
        self.progress.pack(fill=tk.X)
        ttk.Label(body, textvariable=self.progress_text).pack(anchor=tk.E, pady=(4, 0))

        actions = ttk.Frame(body)
        actions.pack(fill=tk.X, pady=(18, 0))
        self.install_button = ttk.Button(actions, text="开始安装", command=self._start_install)
        self.install_button.pack(side=tk.RIGHT)
        ttk.Button(actions, text="取消", command=self.destroy).pack(side=tk.RIGHT, padx=(0, 8))
        self.after(120, self._drain_events)

    def _choose_directory(self) -> None:
        from tkinter import filedialog

        selected = filedialog.askdirectory(initialdir=self.install_path.get())
        if selected:
            self.install_path.set(str(Path(selected) / APP_DIRECTORY_NAME))

    def _start_install(self) -> None:
        if self._installing:
            return
        target = Path(self.install_path.get().strip())
        if not target.name:
            messagebox.showwarning("安装位置无效", "请选择有效的安装位置。")
            return
        self._installing = True
        self.install_button.configure(state=tk.DISABLED)
        threading.Thread(target=self._install, args=(target,), daemon=True).start()

    def _install(self, target: Path) -> None:
        try:
            self._emit("status", "正在读取发布版本信息...")
            manifest = self._load_release_manifest()
            url = str(manifest.get("download_url") or "").strip()
            expected_hash = str(manifest.get("sha256") or "").strip().lower()
            if not url or len(expected_hash) != 64:
                raise RuntimeError("当前发布版本尚未就绪，请稍后重试。")
            with tempfile.TemporaryDirectory(prefix="subtitle-verifier-") as temp:
                archive_path = Path(temp) / str(manifest.get("archive_name") or "app.zip")
                self._download(url, archive_path, int(manifest.get("size_bytes") or 0))
                self._emit("status", "正在校验下载文件...")
                if self._sha256(archive_path) != expected_hash:
                    raise RuntimeError("下载文件校验失败，请检查网络后重试。")
                self._emit("status", "正在安装应用组件...")
                self._extract_and_install(archive_path, target)
            self._create_shortcuts(target)
            self._emit("complete", target)
        except Exception as exc:  # noqa: BLE001
            self._emit("error", str(exc))

    @staticmethod
    def _load_release_manifest() -> dict[str, object]:
        # GitHub API has a low anonymous rate limit. The stable latest-download URL
        # redirects to the newest release asset without consuming that API quota.
        archive_url = f"{RELEASE_DOWNLOAD_BASE_URL}/{ARCHIVE_NAME}"
        hash_url = f"{RELEASE_DOWNLOAD_BASE_URL}/{HASH_NAME}"
        request = urllib.request.Request(hash_url, headers={"User-Agent": "SubtitleVerifierInstaller"})
        with urllib.request.urlopen(request, timeout=20) as response:
            hash_text = response.read().decode("utf-8").strip()
        expected_hash = hash_text.split()[0].lower() if hash_text else ""
        if len(expected_hash) != 64:
            raise RuntimeError("最新发布版本缺少完整性校验文件。")
        payload = {
            "download_url": archive_url,
            "sha256": expected_hash,
            "size_bytes": 0,
            "archive_name": ARCHIVE_NAME,
        }
        if not isinstance(payload, dict):
            raise RuntimeError("发布版本信息格式无效。")
        return payload

    def _download(self, url: str, target: Path, expected_size: int) -> None:
        with urllib.request.urlopen(url, timeout=45) as response, target.open("wb") as output:
            total = int(response.headers.get("Content-Length") or expected_size or 0)
            received = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                received += len(chunk)
                if total:
                    percent = min(100, int(received * 100 / total))
                    self._emit("progress", (percent, f"正在下载组件：{received / 1_048_576:.0f} / {total / 1_048_576:.0f} MB"))
                else:
                    self._emit("progress", (0, f"正在下载组件：{received / 1_048_576:.0f} MB"))

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _extract_and_install(self, archive_path: Path, target: Path) -> None:
        staging = target.parent / f".{target.name}.staging"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(archive_path) as archive:
                root = staging.resolve()
                for member in archive.infolist():
                    destination = (staging / member.filename).resolve()
                    if root not in destination.parents and destination != root:
                        raise RuntimeError("安装包包含无效路径。")
                archive.extractall(staging)
            preserved = staging / ".preserved"
            if target.exists():
                preserved.mkdir()
                for name in ("runtime", "output"):
                    source = target / name
                    if source.exists():
                        shutil.move(str(source), str(preserved / name))
                shutil.rmtree(target)
            shutil.move(str(staging), str(target))
            for name in ("runtime", "output"):
                saved = target / ".preserved" / name
                if saved.exists():
                    shutil.move(str(saved), str(target / name))
            shutil.rmtree(target / ".preserved", ignore_errors=True)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    @staticmethod
    def _create_shortcuts(target: Path) -> None:
        executable = target / APP_EXECUTABLE_NAME
        if not executable.exists():
            raise RuntimeError("安装包中未找到主程序。")
        desktop = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"
        start_menu = Path(os.environ.get("APPDATA", str(Path.home()))) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
        for folder in (desktop, start_menu):
            folder.mkdir(parents=True, exist_ok=True)
            shortcut = folder / "YouTube 字幕核验助手.lnk"
            script = (
                'Set shell = CreateObject("WScript.Shell")\n'
                f'Set link = shell.CreateShortcut("{shortcut}")\n'
                f'link.TargetPath = "{executable}"\n'
                f'link.WorkingDirectory = "{target}"\n'
                f'link.IconLocation = "{executable},0"\n'
                'link.Save\n'
            )
            script_path = target / ".create_shortcut.vbs"
            script_path.write_text(script, encoding="utf-8")
            subprocess.run(["cscript.exe", "//nologo", str(script_path)], check=True, capture_output=True)
            script_path.unlink(missing_ok=True)

    def _emit(self, kind: str, value: object) -> None:
        self._events.put((kind, value))

    def _drain_events(self) -> None:
        try:
            while True:
                kind, value = self._events.get_nowait()
                if kind == "status":
                    self.status_text.set(str(value))
                elif kind == "progress":
                    percent, text = value  # type: ignore[misc]
                    self.progress.configure(value=percent)
                    self.progress_text.set(str(text))
                elif kind == "complete":
                    target = Path(value)
                    self.progress.configure(value=100)
                    self.progress_text.set("安装完成")
                    self.status_text.set("安装完成，已创建桌面和开始菜单快捷方式。")
                    if messagebox.askyesno("安装完成", "YouTube 字幕核验助手已安装完成。是否立即启动？"):
                        subprocess.Popen([str(target / APP_EXECUTABLE_NAME)], cwd=target)
                    self._installing = False
                    self.install_button.configure(state=tk.NORMAL)
                elif kind == "error":
                    self._installing = False
                    self.install_button.configure(state=tk.NORMAL)
                    self.status_text.set("安装失败，可检查网络后重试。")
                    messagebox.showerror("安装失败", str(value))
        except queue.Empty:
            pass
        self.after(120, self._drain_events)


if __name__ == "__main__":
    OnlineInstaller().mainloop()
