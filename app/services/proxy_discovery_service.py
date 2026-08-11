from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class ProxyCandidate:
    address: str
    source: str


class ProxyDiscoveryService:
    """Read user-configured Windows proxy endpoints without changing them."""

    _ENVIRONMENT_KEYS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")

    @classmethod
    def discover(cls) -> tuple[list[ProxyCandidate], bool]:
        candidates: list[ProxyCandidate] = []
        seen: set[str] = set()
        pac_detected = False

        def append(value: object, source: str) -> None:
            address = cls.normalize_proxy(value)
            if address and address not in seen:
                seen.add(address)
                candidates.append(ProxyCandidate(address, source))

        for key in cls._ENVIRONMENT_KEYS:
            append(os.environ.get(key), f"环境变量 {key}")

        registry_values, pac_detected = cls._windows_internet_settings()
        for value in registry_values:
            append(value, "Windows 系统代理")

        for value in cls._winhttp_proxy_values():
            append(value, "WinHTTP 系统代理")
        return candidates, pac_detected

    @staticmethod
    def normalize_proxy(value: object) -> str:
        raw = str(value or "").strip()
        if not raw:
            return ""
        # Windows may store protocol-specific values such as
        # "http=127.0.0.1:7890;https=127.0.0.1:7890".
        protocol_values: dict[str, str] = {}
        for part in raw.split(";"):
            text = part.strip()
            if "=" in text:
                protocol, address = text.split("=", 1)
                protocol_values[protocol.strip().lower()] = address.strip()
            elif text:
                protocol_values.setdefault("default", text)
        raw = protocol_values.get("https") or protocol_values.get("http") or protocol_values.get("default") or ""
        raw = raw.strip().rstrip("/")
        if not raw or raw.lower() in {"direct", "none"}:
            return ""
        if not raw.lower().startswith(("http://", "https://")):
            raw = f"http://{raw}"
        return raw if re.match(r"^https?://[^/:\s]+:\d+$", raw, flags=re.IGNORECASE) else ""

    @staticmethod
    def _windows_internet_settings() -> tuple[list[str], bool]:
        try:
            import winreg
        except ImportError:
            return [], False
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings")
            try:
                enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            except OSError:
                enabled = 0
            try:
                proxy_server, _ = winreg.QueryValueEx(key, "ProxyServer")
            except OSError:
                proxy_server = ""
            try:
                pac_url, _ = winreg.QueryValueEx(key, "AutoConfigURL")
            except OSError:
                pac_url = ""
            return ([str(proxy_server)] if enabled and proxy_server else []), bool(pac_url)
        except OSError:
            return [], False

    @staticmethod
    def _winhttp_proxy_values() -> list[str]:
        if os.name != "nt":
            return []
        try:
            completed = subprocess.run(
                ["netsh", "winhttp", "show", "proxy"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        values: list[str] = []
        for line in completed.stdout.splitlines():
            if ":" not in line:
                continue
            label, value = line.split(":", 1)
            if label.strip().lower() in {"proxy server", "代理服务器"}:
                values.append(value.strip())
        return values


__all__ = ["ProxyCandidate", "ProxyDiscoveryService"]
