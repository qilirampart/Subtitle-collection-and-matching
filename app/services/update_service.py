from __future__ import annotations

import hashlib
import json
import platform
import re
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.version import UPDATE_MANIFEST_URL


class UpdateError(RuntimeError):
    """Raised when an update release cannot be checked or verified safely."""


ProgressCallback = Callable[[int, int], None]


@dataclass(frozen=True)
class UpdateAsset:
    url: str
    sha256_url: str
    file_name: str
    size_bytes: int = 0


@dataclass(frozen=True)
class AvailableUpdate:
    version: str
    release_tag: str
    release_notes_url: str
    asset: UpdateAsset


def version_key(value: str) -> tuple[int, ...]:
    """Turn calendar-style tags such as v2026.08.25.2 into sortable tuples."""
    normalized = str(value or "").strip().lower().removeprefix("v")
    if not normalized:
        return ()
    parts = re.findall(r"\d+", normalized)
    if not parts:
        return ()
    return tuple(int(part) for part in parts)


def is_newer_version(candidate: str, current: str) -> bool:
    candidate_key = version_key(candidate)
    current_key = version_key(current)
    if not candidate_key:
        return False
    length = max(len(candidate_key), len(current_key))
    return candidate_key + (0,) * (length - len(candidate_key)) > current_key + (0,) * (length - len(current_key))


def current_platform_key(machine: str | None = None, system: str | None = None) -> tuple[str, str]:
    system_name = (system or platform.system()).lower()
    machine_name = (machine or platform.machine()).lower()
    if system_name.startswith("win"):
        return "windows", "x64"
    if system_name == "darwin":
        return "macos", "arm64" if machine_name in {"arm64", "aarch64"} else "x64"
    raise UpdateError(f"Unsupported update platform: {system_name or 'unknown'}")


class ApplicationUpdateService:
    def __init__(self, manifest_url: str = UPDATE_MANIFEST_URL) -> None:
        self.manifest_url = manifest_url

    def check_for_update(
        self,
        current_version: str,
        *,
        machine: str | None = None,
        system: str | None = None,
    ) -> AvailableUpdate | None:
        manifest = self._load_manifest()
        version = str(manifest.get("version") or "").strip()
        release_tag = str(manifest.get("release_tag") or "").strip()
        if not version or not release_tag:
            raise UpdateError("Update metadata is missing a version or release tag.")
        if not is_newer_version(version, current_version):
            return None
        platform_name, architecture = current_platform_key(machine, system)
        asset = self._asset_for_platform(manifest, platform_name, architecture)
        release_notes_url = str(manifest.get("release_notes_url") or "").strip()
        return AvailableUpdate(version, release_tag, release_notes_url, asset)

    def download_update(
        self,
        update: AvailableUpdate,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> Path:
        update_dir = Path(tempfile.gettempdir()) / "youtube-subtitle-verifier-updates" / update.version
        update_dir.mkdir(parents=True, exist_ok=True)
        target = update_dir / update.asset.file_name
        temporary = target.with_suffix(f"{target.suffix}.part")
        temporary.unlink(missing_ok=True)
        try:
            self._download(update.asset.url, temporary, update.asset.size_bytes, progress_callback)
            expected_hash = self._read_sha256(update.asset.sha256_url)
            actual_hash = self.sha256(temporary)
            if actual_hash != expected_hash:
                raise UpdateError("Downloaded update verification failed. Please check the network and retry.")
            temporary.replace(target)
            return target
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def _load_manifest(self) -> dict[str, object]:
        request = urllib.request.Request(self.manifest_url, headers={"User-Agent": "YouTubeSubtitleVerifier"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise UpdateError(f"Unable to read update metadata: {exc}") from exc
        if not isinstance(payload, dict) or int(payload.get("schema_version") or 0) != 1:
            raise UpdateError("Update metadata format is invalid.")
        return payload

    @staticmethod
    def _asset_for_platform(manifest: dict[str, object], platform_name: str, architecture: str) -> UpdateAsset:
        root = manifest.get(platform_name)
        raw_asset = root.get(architecture) if isinstance(root, dict) and platform_name == "macos" else root
        if not isinstance(raw_asset, dict):
            raise UpdateError("This release does not provide an update package for this computer.")
        url = str(raw_asset.get("url") or "").strip()
        sha256_url = str(raw_asset.get("sha256_url") or "").strip()
        file_name = str(raw_asset.get("file_name") or Path(url).name).strip()
        if not url or not sha256_url or not file_name:
            raise UpdateError("Update metadata does not include a valid download package.")
        try:
            size_bytes = max(0, int(raw_asset.get("size_bytes") or 0))
        except (TypeError, ValueError):
            size_bytes = 0
        return UpdateAsset(url, sha256_url, file_name, size_bytes)

    @staticmethod
    def _read_sha256(url: str) -> str:
        request = urllib.request.Request(url, headers={"User-Agent": "YouTubeSubtitleVerifier"})
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                value = response.read().decode("utf-8").strip().split(maxsplit=1)[0].lower()
        except OSError as exc:
            raise UpdateError(f"Unable to download update verification file: {exc}") from exc
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            raise UpdateError("Update verification file is invalid.")
        return value

    @staticmethod
    def _download(url: str, target: Path, expected_size: int, callback: ProgressCallback | None) -> None:
        request = urllib.request.Request(url, headers={"User-Agent": "YouTubeSubtitleVerifier"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response, target.open("wb") as output:
                total = int(response.headers.get("Content-Length") or expected_size or 0)
                received = 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    received += len(chunk)
                    if callback:
                        callback(received, total)
        except OSError as exc:
            raise UpdateError(f"Unable to download update package: {exc}") from exc

    @staticmethod
    def sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
