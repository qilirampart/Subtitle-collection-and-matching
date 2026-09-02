from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
import uuid
import zipfile
from pathlib import Path


PRESERVED_DIRECTORIES = ("runtime", "output")


def _wait_for_process(pid: int, timeout_seconds: int = 45) -> bool:
    if pid <= 0:
        return True
    deadline = time.monotonic() + timeout_seconds
    if sys.platform == "win32":
        while time.monotonic() < deadline:
            completed = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                check=False,
            )
            if str(pid) not in completed.stdout:
                return True
            time.sleep(0.4)
        return False
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except PermissionError:
            return True
        time.sleep(0.4)
    return False


def _safe_extract(archive_path: Path, staging: Path) -> None:
    with zipfile.ZipFile(archive_path) as archive:
        root = staging.resolve()
        for member in archive.infolist():
            destination = (staging / member.filename).resolve()
            if root not in destination.parents and destination != root:
                raise RuntimeError("Update package contains an invalid path.")
        archive.extractall(staging)


def _move_preserved_data(source: Path, destination: Path) -> None:
    for name in PRESERVED_DIRECTORIES:
        previous = source / name
        if previous.exists():
            shutil.move(str(previous), str(destination / name))


def _restore_preserved_data(target: Path, preserved: Path) -> None:
    for name in PRESERVED_DIRECTORIES:
        saved = preserved / name
        if saved.exists():
            shutil.rmtree(target / name, ignore_errors=True)
            shutil.move(str(saved), str(target / name))


def update_windows(archive_path: Path, install_root: Path, executable_name: str, pid: int) -> None:
    if not _wait_for_process(pid):
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
        if not _wait_for_process(pid, timeout_seconds=10):
            raise RuntimeError("The running application could not be stopped. Please close it and retry.")
    if not archive_path.is_file():
        raise RuntimeError("Downloaded update package is missing.")
    parent = install_root.parent
    token = uuid.uuid4().hex[:8]
    staging = parent / f".{install_root.name}.update-{token}"
    backup = parent / f".{install_root.name}.backup-{token}"
    preserved = parent / f".{install_root.name}.preserved-{token}"
    shutil.rmtree(staging, ignore_errors=True)
    shutil.rmtree(backup, ignore_errors=True)
    shutil.rmtree(preserved, ignore_errors=True)
    staging.mkdir(parents=True, exist_ok=False)
    try:
        _safe_extract(archive_path, staging)
        if not (staging / executable_name).is_file():
            raise RuntimeError("Update package does not contain the application executable.")
        install_root.mkdir(parents=True, exist_ok=True)
        backup.mkdir(parents=True, exist_ok=True)
        # Replace application files entry by entry. runtime/output stay in
        # place, so an open log or a large cover directory cannot block the
        # update and user data never needs to be moved.
        for entry in list(install_root.iterdir()):
            if entry.name in PRESERVED_DIRECTORIES:
                continue
            shutil.move(str(entry), str(backup / entry.name))
        for entry in list(staging.iterdir()):
            if entry.name in PRESERVED_DIRECTORIES:
                continue
            shutil.move(str(entry), str(install_root / entry.name))
        subprocess.Popen([str(install_root / executable_name)], cwd=str(install_root))
        shutil.rmtree(backup, ignore_errors=True)
    except Exception:
        if backup.exists():
            for entry in list(install_root.iterdir()) if install_root.exists() else []:
                if entry.name not in PRESERVED_DIRECTORIES:
                    if entry.is_dir():
                        shutil.rmtree(entry, ignore_errors=True)
                    else:
                        entry.unlink(missing_ok=True)
            for entry in list(backup.iterdir()):
                shutil.move(str(entry), str(install_root / entry.name))
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(preserved, ignore_errors=True)
        archive_path.unlink(missing_ok=True)


def _mount_application(dmg_path: Path) -> tuple[Path, Path]:
    attached = subprocess.check_output(
        ["hdiutil", "attach", "-nobrowse", "-readonly", "-plist", str(dmg_path)],
        stderr=subprocess.STDOUT,
    )
    payload = plistlib.loads(attached)
    entities = payload.get("system-entities") if isinstance(payload, dict) else []
    mount_point = next(
        (Path(str(entity.get("mount-point"))) for entity in entities or [] if isinstance(entity, dict) and entity.get("mount-point")),
        None,
    )
    if mount_point is None:
        raise RuntimeError("Unable to mount the macOS update package.")
    app_bundle = next((candidate for candidate in mount_point.glob("*.app") if candidate.is_dir()), None)
    if app_bundle is None:
        subprocess.run(["hdiutil", "detach", str(mount_point), "-quiet"], check=False)
        raise RuntimeError("The macOS update package does not contain an app bundle.")
    return app_bundle, mount_point


def _detach(mount_point: Path) -> None:
    subprocess.run(["hdiutil", "detach", str(mount_point), "-quiet"], check=False)


def _run_privileged_shell(command: str) -> None:
    completed = subprocess.run(
        ["osascript", "-e", f"do shell script {json.dumps(command)} with administrator privileges"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(detail or "Administrator permission was not granted for the update.")


def update_macos(archive_path: Path, app_bundle: Path, pid: int) -> None:
    _wait_for_process(pid)
    if not archive_path.is_file():
        raise RuntimeError("Downloaded update package is missing.")
    work_dir = Path("/tmp") / f"youtube-subtitle-verifier-update-{uuid.uuid4().hex[:8]}"
    preserve_dir = work_dir / "preserved"
    work_dir.mkdir(parents=True, exist_ok=False)
    mount_point: Path | None = None
    try:
        new_bundle, mount_point = _mount_application(archive_path)
        backup = app_bundle.with_name(f".{app_bundle.name}.backup")
        import shlex

        q = lambda value: shlex.quote(str(value))
        # On macOS the application stores runtime and output under Application
        # Support, outside the app bundle, so replacing this bundle keeps all
        # user configuration and task data intact.
        command = (
            f"set -e; rm -rf {q(backup)}; "
            f"if [ -d {q(app_bundle)} ]; then mv {q(app_bundle)} {q(backup)}; fi; "
            f"if ! ditto {q(new_bundle)} {q(app_bundle)}; then "
            f"rm -rf {q(app_bundle)}; [ -d {q(backup)} ] && mv {q(backup)} {q(app_bundle)}; exit 1; fi; "
            f"rm -rf {q(backup)}; open {q(app_bundle)}"
        )
        _run_privileged_shell(command)
    finally:
        if mount_point is not None:
            _detach(mount_point)
        shutil.rmtree(work_dir, ignore_errors=True)
        archive_path.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="YouTube Subtitle Verifier updater")
    parser.add_argument("--platform", choices=("windows", "macos"), required=True)
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--install-root", type=Path, required=True)
    parser.add_argument("--executable-name", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.platform == "windows":
        if not args.executable_name:
            raise RuntimeError("Missing Windows application executable name.")
        update_windows(args.archive, args.install_root, args.executable_name, args.pid)
    else:
        update_macos(args.archive, args.install_root, args.pid)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        # The updater is started after the GUI exits. A visible error dialog is
        # more actionable than silently leaving the old application in place.
        if sys.platform == "win32":
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, str(exc), "Update failed", 0x10)
        else:
            subprocess.run(
                ["osascript", "-e", f"display alert \"Update failed\" message {json.dumps(str(exc))}"],
                check=False,
            )
        # The actionable error has already been shown to the user. Exit with
        # a failure status without printing a PyInstaller traceback window.
        raise SystemExit(1)
