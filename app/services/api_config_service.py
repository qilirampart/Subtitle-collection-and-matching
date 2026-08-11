from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from app.config.settings import API_CONFIG_PATH, DOWNLOADER_CONFIG_PATH, TENCENT_ASR_CONFIG_PATH
from app.utils.logger import get_logger

DEFAULT_TENCENT_ASR_PROVIDER: dict[str, Any] = {
    "name": "Tencent ASR Primary",
    "provider": "tencent_asr",
    "enabled": True,
    "priority": 1,
    "secret_id": "",
    "secret_key": "",
    "region": "ap-shanghai",
    "engine_model_type": "16k_zh",
    "res_text_format": 3,
    "channel_num": 1,
}

DEFAULT_DOUBAO_ASR_PROVIDER: dict[str, Any] = {
    "name": "Doubao ASR Backup",
    "provider": "doubao_asr",
    "enabled": False,
    "priority": 2,
    "api_key": "",
    "app_id": "",
    "access_token": "",
    "resource_id": "",
    "ws_url": "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_nostream",
    "language": "zh-CN",
    "model_name": "bigmodel",
    "audio_format": "mp3",
    "sample_rate": 16000,
    "bits": 16,
    "channel_num": 1,
    "show_utterances": True,
    "enable_itn": True,
    "enable_punc": True,
    "result_type": "full",
    "uid": "",
}

DEFAULT_ASR_PROVIDER: dict[str, Any] = deepcopy(DEFAULT_TENCENT_ASR_PROVIDER)

DEFAULT_DOUYIN_PARSER_PROVIDER: dict[str, Any] = {
    "name": "Douyin Parser Primary",
    "enabled": True,
    "priority": 1,
    "base_url": "https://douyin-vd.vercel.app/api/hello",
}

DEFAULT_VIDEO_REVIEW_CONFIG: dict[str, Any] = {
    "enabled": True,
    "profile_id": "",
    "api_base": "https://api.lemondata.cc",
    "api_key": "",
    "model": "gemini-3.5-flash",
    "chunk_seconds": 30,
    "upload_width": 360,
    "temperature": 0,
    "timeout_seconds": 180,
}

DEFAULT_TEXT_CORRECTION_CONFIG: dict[str, Any] = {
    "enabled": False,
    "profile_id": "",
    "api_base": "https://api.lemondata.cc",
    "api_key": "",
    "model": "gemini-3.5-flash",
    "temperature": 0,
    "timeout_seconds": 90,
    "max_chars_per_chunk": 1800,
    "batch_items_per_request": 5,
}

DEFAULT_LLM_PROFILE: dict[str, Any] = {
    "id": "llm_profile_1",
    "name": "OpenAI Compatible 1",
    "enabled": True,
    "provider": "openai_compatible",
    "api_base": "https://api.lemondata.cc",
    "api_key": "",
    "model": "gemini-3.5-flash",
    "temperature": 0,
}

DEFAULT_MATCHING_SERVICE_CONFIG: dict[str, Any] = {
    "base_url": "http://novel-similarity-dev.dzkjm.cn",
    "username": "",
    "password": "",
    "timeout_seconds": 45,
}

DEFAULT_API_CONFIG: dict[str, Any] = {
    "asr": {
        "enabled": True,
        "failover": {
            "failure_threshold": 1,
            "cooldown_seconds": 300,
        },
        "providers": [
            deepcopy(DEFAULT_TENCENT_ASR_PROVIDER),
            deepcopy(DEFAULT_DOUBAO_ASR_PROVIDER),
        ],
    },
    "douyin_parser": {
        "enabled": True,
        "failover": {
            "failure_threshold": 2,
            "cooldown_seconds": 300,
        },
        "providers": [deepcopy(DEFAULT_DOUYIN_PARSER_PROVIDER)],
    },
    "video_review": deepcopy(DEFAULT_VIDEO_REVIEW_CONFIG),
    "text_correction": deepcopy(DEFAULT_TEXT_CORRECTION_CONFIG),
    "llm": {
        "profiles": [deepcopy(DEFAULT_LLM_PROFILE)],
    },
    "matching_service": deepcopy(DEFAULT_MATCHING_SERVICE_CONFIG),
}


class ApiConfigService:
    ASR_PROVIDER_LABELS = {
        "tencent_asr": "腾讯云 ASR",
        "doubao_asr": "豆包 ASR",
    }

    def __init__(self) -> None:
        self._config: dict[str, Any] | None = None
        self._logger = get_logger(__name__)

    def load_config(self, *, force_reload: bool = False) -> dict[str, Any]:
        if self._config is not None and not force_reload:
            return deepcopy(self._config)

        if API_CONFIG_PATH.exists():
            raw = self._read_json(API_CONFIG_PATH) or {}
            source = str(API_CONFIG_PATH)
        else:
            raw = self._build_from_legacy_configs()
            source = "legacy runtime configs"

        normalized = self.normalize_config(raw)
        self._persist(normalized)
        self._config = normalized
        self._logger.info("Loaded API configuration from %s", source)
        return deepcopy(normalized)

    def save_config(self, config: dict[str, Any]) -> dict[str, Any]:
        normalized = self.normalize_config(config)
        self._persist(normalized)
        self._config = normalized
        return deepcopy(normalized)

    def list_asr_providers(
        self,
        *,
        include_disabled: bool = False,
        require_secret: bool = False,
    ) -> list[dict[str, Any]]:
        providers = self.load_config().get("asr", {}).get("providers", [])
        ordered = self._sort_by_priority(providers)
        result: list[dict[str, Any]] = []
        for provider in ordered:
            if not include_disabled and not provider.get("enabled", True):
                continue
            if require_secret and not self.is_asr_provider_ready(provider):
                continue
            result.append(provider)
        return result

    def list_douyin_parser_providers(self, *, include_disabled: bool = False) -> list[dict[str, Any]]:
        providers = self.load_config().get("douyin_parser", {}).get("providers", [])
        ordered = self._sort_by_priority(providers)
        if include_disabled:
            return ordered
        return [provider for provider in ordered if provider.get("enabled", True)]

    def get_video_review_config(self) -> dict[str, Any]:
        return deepcopy(self.load_config().get("video_review", DEFAULT_VIDEO_REVIEW_CONFIG))

    def get_text_correction_config(self) -> dict[str, Any]:
        return deepcopy(self.load_config().get("text_correction", DEFAULT_TEXT_CORRECTION_CONFIG))

    def get_matching_service_config(self) -> dict[str, Any]:
        return deepcopy(self.load_config().get("matching_service", DEFAULT_MATCHING_SERVICE_CONFIG))

    def get_llm_profiles(self, *, include_disabled: bool = False) -> list[dict[str, Any]]:
        profiles = self.load_config().get("llm", {}).get("profiles", [])
        normalized = self._normalize_llm_profiles(profiles if isinstance(profiles, list) else [])
        if include_disabled:
            return normalized
        return [profile for profile in normalized if profile.get("enabled", True)]

    def save_video_review_config(self, config: dict[str, Any]) -> dict[str, Any]:
        payload = self.load_config(force_reload=True)
        payload["video_review"] = self._normalize_video_review_config(config)
        return self.save_config(payload).get("video_review", {})

    def save_text_correction_config(self, config: dict[str, Any]) -> dict[str, Any]:
        payload = self.load_config(force_reload=True)
        payload["text_correction"] = self._normalize_text_correction_config(config)
        return self.save_config(payload).get("text_correction", {})

    def save_matching_service_config(self, config: dict[str, Any]) -> dict[str, Any]:
        payload = self.load_config(force_reload=True)
        payload["matching_service"] = self._normalize_matching_service_config(config)
        return self.save_config(payload).get("matching_service", {})

    def is_matching_service_ready(self, config: dict[str, Any] | None = None) -> bool:
        payload = self._normalize_matching_service_config(config or self.get_matching_service_config())
        return bool(payload["base_url"] and payload["username"] and payload["password"])

    def save_llm_profiles(self, profiles: list[dict[str, Any]]) -> list[dict[str, Any]]:
        payload = self.load_config(force_reload=True)
        payload.setdefault("llm", {})
        payload["llm"]["profiles"] = self._normalize_llm_profiles(profiles)
        return self.save_config(payload).get("llm", {}).get("profiles", [])

    def is_video_review_ready(
        self,
        config: dict[str, Any] | None = None,
        *,
        config_root: dict[str, Any] | None = None,
    ) -> bool:
        payload = self.resolve_video_review_config(config or self.get_video_review_config(), config=config_root)
        if payload.get("profile_id") and not payload.get("llm_profile_enabled", False):
            return False
        return bool(payload.get("enabled", True) and payload.get("api_base") and payload.get("api_key") and payload.get("model"))

    def is_text_correction_ready(
        self,
        config: dict[str, Any] | None = None,
        *,
        config_root: dict[str, Any] | None = None,
    ) -> bool:
        payload = self.resolve_text_correction_config(config or self.get_text_correction_config(), config=config_root)
        if payload.get("profile_id") and not payload.get("llm_profile_enabled", False):
            return False
        return bool(payload.get("enabled", False) and payload.get("api_base") and payload.get("api_key") and payload.get("model"))

    def normalize_text_correction_config(self, raw: dict[str, Any] | None) -> dict[str, Any]:
        return self._normalize_text_correction_config(raw)

    def create_default_llm_profile(self, *, index: int) -> dict[str, Any]:
        profile = deepcopy(DEFAULT_LLM_PROFILE)
        safe_index = max(1, int(index))
        profile["id"] = f"llm_profile_{safe_index}"
        profile["name"] = f"OpenAI Compatible {safe_index}"
        return profile

    def get_llm_profile(self, profile_id: str, *, config: dict[str, Any] | None = None) -> dict[str, Any] | None:
        target_id = str(profile_id or "").strip()
        if not target_id:
            return None
        profiles = (config or self.load_config()).get("llm", {}).get("profiles", [])
        for profile in self._normalize_llm_profiles(profiles if isinstance(profiles, list) else []):
            if str(profile.get("id") or "").strip() == target_id:
                return profile
        return None

    def is_llm_profile_ready(self, profile: dict[str, Any]) -> bool:
        return bool(
            profile.get("enabled", True)
            and str(profile.get("api_base") or "").strip()
            and str(profile.get("api_key") or "").strip()
            and str(profile.get("model") or "").strip()
        )

    def resolve_text_correction_config(
        self,
        raw: dict[str, Any] | None = None,
        *,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = self._normalize_text_correction_config(raw if raw is not None else self.get_text_correction_config())
        return self._merge_llm_profile(payload, config=config)

    def resolve_video_review_config(
        self,
        raw: dict[str, Any] | None = None,
        *,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = self._normalize_video_review_config(raw if raw is not None else self.get_video_review_config())
        return self._merge_llm_profile(payload, config=config)

    def normalize_config(self, raw: dict[str, Any] | None) -> dict[str, Any]:
        incoming = raw if isinstance(raw, dict) else {}
        payload = deepcopy(DEFAULT_API_CONFIG)

        asr_raw = incoming.get("asr")
        if isinstance(asr_raw, dict):
            payload["asr"]["enabled"] = bool(asr_raw.get("enabled", payload["asr"]["enabled"]))
            payload["asr"]["failover"] = self._normalize_failover(
                asr_raw.get("failover"),
                default=payload["asr"]["failover"],
            )
            if "providers" in asr_raw:
                providers_raw = asr_raw.get("providers")
                payload["asr"]["providers"] = self._normalize_asr_providers(
                    providers_raw if isinstance(providers_raw, list) else []
                )

        douyin_raw = incoming.get("douyin_parser")
        if isinstance(douyin_raw, dict):
            payload["douyin_parser"]["enabled"] = bool(
                douyin_raw.get("enabled", payload["douyin_parser"]["enabled"])
            )
            payload["douyin_parser"]["failover"] = self._normalize_failover(
                douyin_raw.get("failover"),
                default=payload["douyin_parser"]["failover"],
            )
            if "providers" in douyin_raw:
                providers_raw = douyin_raw.get("providers")
                payload["douyin_parser"]["providers"] = self._normalize_douyin_parser_providers(
                    providers_raw if isinstance(providers_raw, list) else []
                )

        llm_raw = incoming.get("llm")
        if isinstance(llm_raw, dict) and "profiles" in llm_raw:
            profiles_raw = llm_raw.get("profiles")
            payload["llm"]["profiles"] = self._normalize_llm_profiles(profiles_raw if isinstance(profiles_raw, list) else [])

        video_review_raw = incoming.get("video_review")
        if isinstance(video_review_raw, dict):
            payload["video_review"] = self._normalize_video_review_config(video_review_raw)

        text_correction_raw = incoming.get("text_correction")
        if isinstance(text_correction_raw, dict):
            payload["text_correction"] = self._normalize_text_correction_config(text_correction_raw)

        matching_service_raw = incoming.get("matching_service")
        if isinstance(matching_service_raw, dict):
            payload["matching_service"] = self._normalize_matching_service_config(matching_service_raw)

        return payload

    def is_asr_provider_ready(self, provider: dict[str, Any]) -> bool:
        provider_type = str(provider.get("provider") or "tencent_asr").strip()
        if provider_type == "doubao_asr":
            api_key = str(provider.get("api_key") or "").strip()
            resource_id = str(provider.get("resource_id") or "").strip()
            if api_key and resource_id:
                return True
            return bool(
                str(provider.get("app_id") or "").strip()
                and str(provider.get("access_token") or "").strip()
                and resource_id
            )
        return bool(
            str(provider.get("secret_id") or "").strip()
            and str(provider.get("secret_key") or "").strip()
        )

    def get_asr_provider_label(self, provider_type: str) -> str:
        return self.ASR_PROVIDER_LABELS.get(provider_type, provider_type or "ASR")

    def create_default_asr_provider(self, provider_type: str, *, priority: int) -> dict[str, Any]:
        normalized_type = str(provider_type or "tencent_asr").strip() or "tencent_asr"
        defaults = self._asr_provider_defaults(normalized_type)
        provider = deepcopy(defaults)
        provider["priority"] = max(1, int(priority))
        provider["name"] = self._fallback_asr_provider_name(normalized_type, provider["priority"])
        return provider

    @staticmethod
    def _sort_by_priority(providers: list[dict[str, Any]]) -> list[dict[str, Any]]:
        indexed = list(enumerate(providers))
        indexed.sort(key=lambda item: (int(item[1].get("priority", item[0] + 1)), item[0]))
        return [provider for _, provider in indexed]

    def _build_from_legacy_configs(self) -> dict[str, Any]:
        payload = deepcopy(DEFAULT_API_CONFIG)

        legacy_asr = self._read_json(TENCENT_ASR_CONFIG_PATH)
        if isinstance(legacy_asr, dict):
            payload["asr"]["enabled"] = bool(legacy_asr.get("enabled", payload["asr"]["enabled"]))
            payload["asr"]["providers"] = [
                self._normalize_asr_provider(
                    {
                        **deepcopy(DEFAULT_TENCENT_ASR_PROVIDER),
                        **legacy_asr,
                    },
                    index=1,
                    used_names=set(),
                ),
                deepcopy(DEFAULT_DOUBAO_ASR_PROVIDER),
            ]

        legacy_downloader = self._read_json(DOWNLOADER_CONFIG_PATH)
        if isinstance(legacy_downloader, dict):
            parser_base_url = str(legacy_downloader.get("parser_base_url") or "").strip()
            provider = deepcopy(payload["douyin_parser"]["providers"][0])
            if parser_base_url:
                provider["base_url"] = parser_base_url
            payload["douyin_parser"]["enabled"] = bool(
                legacy_downloader.get("enabled", payload["douyin_parser"]["enabled"])
            )
            payload["douyin_parser"]["providers"] = [
                self._normalize_douyin_parser_provider(provider, index=1, used_names=set())
            ]

        self._logger.info("Prepared API configuration from legacy runtime files.")
        return payload

    def _normalize_asr_providers(self, providers: list[Any]) -> list[dict[str, Any]]:
        used_names: set[str] = set()
        normalized: list[dict[str, Any]] = []
        for index, provider in enumerate(providers, start=1):
            if not isinstance(provider, dict):
                continue
            normalized.append(self._normalize_asr_provider(provider, index=index, used_names=used_names))
        return self._ensure_builtin_asr_providers(normalized, used_names=used_names)

    def _ensure_builtin_asr_providers(
        self,
        providers: list[dict[str, Any]],
        *,
        used_names: set[str],
    ) -> list[dict[str, Any]]:
        normalized = self._sort_by_priority(providers)
        existing_types = {str(provider.get("provider") or "").strip() for provider in normalized}
        next_priority = max(
            (self._normalize_positive_int(provider.get("priority"), default=index) for index, provider in enumerate(normalized, start=1)),
            default=0,
        )

        for provider_type in ("tencent_asr", "doubao_asr"):
            if provider_type in existing_types:
                continue
            next_priority += 1
            provider = self.create_default_asr_provider(provider_type, priority=next_priority)
            normalized.append(
                self._normalize_asr_provider(
                    provider,
                    index=next_priority,
                    used_names=used_names,
                )
            )
            existing_types.add(provider_type)

        return self._sort_by_priority(normalized)

    def _normalize_douyin_parser_providers(self, providers: list[Any]) -> list[dict[str, Any]]:
        used_names: set[str] = set()
        normalized: list[dict[str, Any]] = []
        for index, provider in enumerate(providers, start=1):
            if not isinstance(provider, dict):
                continue
            normalized.append(self._normalize_douyin_parser_provider(provider, index=index, used_names=used_names))
        return normalized

    def _normalize_asr_provider(
        self,
        provider: dict[str, Any],
        *,
        index: int,
        used_names: set[str],
    ) -> dict[str, Any]:
        provider_type = str(provider.get("provider") or DEFAULT_TENCENT_ASR_PROVIDER["provider"]).strip()
        payload = self._asr_provider_defaults(provider_type)
        payload.update(provider)
        payload["provider"] = provider_type or DEFAULT_TENCENT_ASR_PROVIDER["provider"]
        payload["enabled"] = bool(payload.get("enabled", True))
        payload["priority"] = self._normalize_positive_int(payload.get("priority"), default=index)
        payload["name"] = self._build_unique_name(
            payload.get("name"),
            fallback=self._fallback_asr_provider_name(payload["provider"], index),
            used_names=used_names,
        )

        if payload["provider"] == "doubao_asr":
            payload["api_key"] = str(
                payload.get("api_key") or payload.get("x_api_key") or payload.get("apiKey") or ""
            ).strip()
            payload["app_id"] = str(payload.get("app_id") or payload.get("appid") or "").strip()
            payload["access_token"] = str(
                payload.get("access_token") or payload.get("access_key") or payload.get("token") or ""
            ).strip()
            payload["resource_id"] = str(payload.get("resource_id") or "").strip()
            payload["ws_url"] = str(payload.get("ws_url") or DEFAULT_DOUBAO_ASR_PROVIDER["ws_url"]).strip()
            payload["language"] = str(payload.get("language") or DEFAULT_DOUBAO_ASR_PROVIDER["language"]).strip()
            payload["model_name"] = str(
                payload.get("model_name") or DEFAULT_DOUBAO_ASR_PROVIDER["model_name"]
            ).strip()
            payload["audio_format"] = str(
                payload.get("audio_format") or DEFAULT_DOUBAO_ASR_PROVIDER["audio_format"]
            ).strip()
            payload["sample_rate"] = self._normalize_positive_int(
                payload.get("sample_rate"),
                default=int(DEFAULT_DOUBAO_ASR_PROVIDER["sample_rate"]),
            )
            payload["bits"] = self._normalize_positive_int(
                payload.get("bits"),
                default=int(DEFAULT_DOUBAO_ASR_PROVIDER["bits"]),
            )
            payload["channel_num"] = 1 if self._normalize_int(payload.get("channel_num"), default=1) != 2 else 2
            payload["show_utterances"] = bool(payload.get("show_utterances", True))
            payload["enable_itn"] = bool(payload.get("enable_itn", True))
            payload["enable_punc"] = bool(payload.get("enable_punc", True))
            payload["result_type"] = str(
                payload.get("result_type") or DEFAULT_DOUBAO_ASR_PROVIDER["result_type"]
            ).strip() or "full"
            payload["uid"] = str(payload.get("uid") or "").strip()
            return payload

        payload["secret_id"] = str(payload.get("secret_id") or "").strip()
        payload["secret_key"] = str(payload.get("secret_key") or "").strip()
        payload["region"] = str(payload.get("region") or DEFAULT_TENCENT_ASR_PROVIDER["region"]).strip()
        payload["engine_model_type"] = str(
            payload.get("engine_model_type") or DEFAULT_TENCENT_ASR_PROVIDER["engine_model_type"]
        ).strip()
        payload["res_text_format"] = self._normalize_int(payload.get("res_text_format"), default=3)
        payload["channel_num"] = 1 if self._normalize_int(payload.get("channel_num"), default=1) != 2 else 2
        return payload

    def _normalize_douyin_parser_provider(
        self,
        provider: dict[str, Any],
        *,
        index: int,
        used_names: set[str],
    ) -> dict[str, Any]:
        payload = deepcopy(DEFAULT_DOUYIN_PARSER_PROVIDER)
        payload.update(provider)
        payload["name"] = self._build_unique_name(
            payload.get("name"),
            fallback=f"Douyin Parser {index}",
            used_names=used_names,
        )
        payload["enabled"] = bool(payload.get("enabled", True))
        payload["priority"] = self._normalize_positive_int(payload.get("priority"), default=index)
        payload["base_url"] = str(payload.get("base_url") or "").strip()
        return payload

    def _normalize_llm_profiles(self, profiles: list[Any]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        used_ids: set[str] = set()
        used_names: set[str] = set()
        for index, profile in enumerate(profiles, start=1):
            if not isinstance(profile, dict):
                continue
            normalized.append(self._normalize_llm_profile(profile, index=index, used_ids=used_ids, used_names=used_names))
        if normalized:
            return normalized
        return [
            self._normalize_llm_profile(
                deepcopy(DEFAULT_LLM_PROFILE),
                index=1,
                used_ids=used_ids,
                used_names=used_names,
            )
        ]

    def _normalize_llm_profile(
        self,
        raw: dict[str, Any],
        *,
        index: int,
        used_ids: set[str],
        used_names: set[str],
    ) -> dict[str, Any]:
        payload = deepcopy(DEFAULT_LLM_PROFILE)
        payload.update(raw)
        payload["enabled"] = bool(payload.get("enabled", True))
        payload["provider"] = str(payload.get("provider") or DEFAULT_LLM_PROFILE["provider"]).strip() or DEFAULT_LLM_PROFILE["provider"]
        payload["api_base"] = str(payload.get("api_base") or DEFAULT_LLM_PROFILE["api_base"]).strip().rstrip("/")
        payload["api_key"] = str(payload.get("api_key") or payload.get("token") or "").strip()
        payload["model"] = str(payload.get("model") or DEFAULT_LLM_PROFILE["model"]).strip()
        payload["temperature"] = self._normalize_float(payload.get("temperature"), default=float(DEFAULT_LLM_PROFILE["temperature"]))
        payload["temperature"] = max(0, min(1, payload["temperature"]))

        base_id = str(payload.get("id") or f"llm_profile_{index}").strip() or f"llm_profile_{index}"
        candidate_id = base_id
        suffix = 2
        while candidate_id in used_ids:
            candidate_id = f"{base_id}_{suffix}"
            suffix += 1
        used_ids.add(candidate_id)
        payload["id"] = candidate_id
        payload["name"] = self._build_unique_name(
            payload.get("name"),
            fallback=f"OpenAI Compatible {index}",
            used_names=used_names,
        )
        return payload

    def _merge_llm_profile(
        self,
        payload: dict[str, Any],
        *,
        config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        merged = dict(payload)
        profile_id = str(merged.get("profile_id") or "").strip()
        if not profile_id:
            return merged
        profile = self.get_llm_profile(profile_id, config=config)
        if profile is None:
            merged["llm_profile_enabled"] = False
            return merged
        merged["llm_profile_enabled"] = bool(profile.get("enabled", True))
        merged["api_base"] = str(profile.get("api_base") or merged.get("api_base") or "").strip().rstrip("/")
        merged["api_key"] = str(profile.get("api_key") or merged.get("api_key") or "").strip()
        merged["model"] = str(profile.get("model") or merged.get("model") or "").strip()
        merged["temperature"] = profile.get("temperature", merged.get("temperature", 0))
        merged["llm_profile_name"] = str(profile.get("name") or "")
        merged["llm_profile_provider"] = str(profile.get("provider") or "")
        return merged

    def _normalize_video_review_config(self, raw: dict[str, Any] | None) -> dict[str, Any]:
        source = raw if isinstance(raw, dict) else {}
        payload = deepcopy(DEFAULT_VIDEO_REVIEW_CONFIG)
        payload.update(source)
        payload["enabled"] = bool(payload.get("enabled", True))
        payload["profile_id"] = str(payload.get("profile_id") or "").strip()
        payload["api_base"] = str(payload.get("api_base") or DEFAULT_VIDEO_REVIEW_CONFIG["api_base"]).strip().rstrip("/")
        payload["api_key"] = str(payload.get("api_key") or "").strip()
        payload["model"] = str(payload.get("model") or DEFAULT_VIDEO_REVIEW_CONFIG["model"]).strip()
        payload["chunk_seconds"] = max(
            5,
            min(120, self._normalize_int(payload.get("chunk_seconds"), default=int(DEFAULT_VIDEO_REVIEW_CONFIG["chunk_seconds"]))),
        )
        payload["upload_width"] = max(
            240,
            min(1080, self._normalize_int(payload.get("upload_width"), default=int(DEFAULT_VIDEO_REVIEW_CONFIG["upload_width"]))),
        )
        payload["timeout_seconds"] = max(
            30,
            min(600, self._normalize_int(payload.get("timeout_seconds"), default=int(DEFAULT_VIDEO_REVIEW_CONFIG["timeout_seconds"]))),
        )
        try:
            payload["temperature"] = float(payload.get("temperature", 0) or 0)
        except (TypeError, ValueError):
            payload["temperature"] = 0
        payload["temperature"] = max(0, min(1, payload["temperature"]))
        return payload

    def _normalize_text_correction_config(self, raw: dict[str, Any] | None) -> dict[str, Any]:
        source = raw if isinstance(raw, dict) else {}
        payload = deepcopy(DEFAULT_TEXT_CORRECTION_CONFIG)
        payload.update(source)
        payload["enabled"] = bool(payload.get("enabled", False))
        payload["profile_id"] = str(payload.get("profile_id") or "").strip()
        payload["api_base"] = str(payload.get("api_base") or DEFAULT_TEXT_CORRECTION_CONFIG["api_base"]).strip().rstrip("/")
        payload["api_key"] = str(payload.get("api_key") or "").strip()
        payload["model"] = str(payload.get("model") or DEFAULT_TEXT_CORRECTION_CONFIG["model"]).strip()
        payload["timeout_seconds"] = max(
            15,
            min(
                600,
                self._normalize_int(
                    payload.get("timeout_seconds"),
                    default=int(DEFAULT_TEXT_CORRECTION_CONFIG["timeout_seconds"]),
                ),
            ),
        )
        payload["max_chars_per_chunk"] = max(
            800,
            min(
                6000,
                self._normalize_int(
                    payload.get("max_chars_per_chunk"),
                    default=int(DEFAULT_TEXT_CORRECTION_CONFIG["max_chars_per_chunk"]),
                ),
            ),
        )
        payload["batch_items_per_request"] = max(
            1,
            min(
                10,
                self._normalize_int(
                    payload.get("batch_items_per_request"),
                    default=int(DEFAULT_TEXT_CORRECTION_CONFIG["batch_items_per_request"]),
                ),
            ),
        )
        try:
            payload["temperature"] = float(payload.get("temperature", 0) or 0)
        except (TypeError, ValueError):
            payload["temperature"] = 0
        payload["temperature"] = max(0, min(1, payload["temperature"]))
        return payload

    def _normalize_matching_service_config(self, raw: dict[str, Any] | None) -> dict[str, Any]:
        source = raw if isinstance(raw, dict) else {}
        payload = deepcopy(DEFAULT_MATCHING_SERVICE_CONFIG)
        payload.update(source)
        payload["base_url"] = str(
            payload.get("base_url") or DEFAULT_MATCHING_SERVICE_CONFIG["base_url"]
        ).strip().rstrip("/")
        payload["username"] = str(payload.get("username") or "").strip()
        payload["password"] = str(payload.get("password") or "")
        payload["timeout_seconds"] = max(
            10,
            min(
                180,
                self._normalize_int(
                    payload.get("timeout_seconds"),
                    default=int(DEFAULT_MATCHING_SERVICE_CONFIG["timeout_seconds"]),
                ),
            ),
        )
        return payload

    def _asr_provider_defaults(self, provider_type: str) -> dict[str, Any]:
        normalized_type = str(provider_type or "tencent_asr").strip()
        if normalized_type == "doubao_asr":
            return deepcopy(DEFAULT_DOUBAO_ASR_PROVIDER)
        return deepcopy(DEFAULT_TENCENT_ASR_PROVIDER)

    def _fallback_asr_provider_name(self, provider_type: str, index: int) -> str:
        if provider_type == "doubao_asr":
            return f"Doubao ASR {index}"
        return f"Tencent ASR {index}"

    def _persist(self, config: dict[str, Any]) -> None:
        API_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        API_CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        self._logger.info("Saved API configuration to %s", API_CONFIG_PATH)

    @staticmethod
    def _normalize_failover(raw: Any, *, default: dict[str, Any]) -> dict[str, int]:
        payload = default if isinstance(default, dict) else {"failure_threshold": 1, "cooldown_seconds": 300}
        source = raw if isinstance(raw, dict) else {}
        return {
            "failure_threshold": max(
                1,
                ApiConfigService._normalize_int(
                    source.get("failure_threshold"),
                    default=int(payload.get("failure_threshold", 1)),
                ),
            ),
            "cooldown_seconds": max(
                0,
                ApiConfigService._normalize_int(
                    source.get("cooldown_seconds"),
                    default=int(payload.get("cooldown_seconds", 300)),
                ),
            ),
        }

    @staticmethod
    def _build_unique_name(value: Any, *, fallback: str, used_names: set[str]) -> str:
        base = str(value or fallback).strip() or fallback
        candidate = base
        suffix = 2
        while candidate in used_names:
            candidate = f"{base} #{suffix}"
            suffix += 1
        used_names.add(candidate)
        return candidate

    @staticmethod
    def _normalize_int(value: Any, *, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _normalize_positive_int(value: Any, *, default: int) -> int:
        return max(1, ApiConfigService._normalize_int(value, default=default))

    @staticmethod
    def _normalize_float(value: Any, *, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _read_json(self, path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            self._logger.warning("Failed to read JSON config %s: %s", path, exc)
            return None
