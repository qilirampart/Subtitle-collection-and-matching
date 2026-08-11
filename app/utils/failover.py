from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from app.utils.logger import get_logger


@dataclass
class CircuitState:
    consecutive_failures: int = 0
    opened_until: float = 0.0
    last_error: str = ""
    last_failure_at: float = 0.0
    last_success_at: float = 0.0


class FailoverRouter:
    def __init__(self, group: str, *, logger_name: str | None = None) -> None:
        self._group = group
        self._logger = get_logger(logger_name or __name__)
        self._states: dict[str, CircuitState] = {}

    def ordered_candidates(
        self,
        providers: list[dict[str, Any]],
        *,
        failure_threshold: int,
        cooldown_seconds: int,
    ) -> list[dict[str, Any]]:
        del failure_threshold, cooldown_seconds
        indexed = list(enumerate(providers))
        indexed.sort(key=lambda item: (int(item[1].get("priority", item[0] + 1)), item[0]))

        available: list[dict[str, Any]] = []
        open_circuits: list[dict[str, Any]] = []
        now = time.time()

        for index, provider in indexed:
            provider_name = self._provider_name(provider, index)
            state = self._states.get(provider_name)
            if state is not None and state.opened_until > now:
                remaining = int(max(state.opened_until - now, 0))
                self._logger.warning(
                    "[%s] Skipping provider=%s because circuit is open for %ss. last_error=%s",
                    self._group,
                    provider_name,
                    remaining,
                    state.last_error or "-",
                )
                open_circuits.append(provider)
                continue
            available.append(provider)

        if available:
            return available
        if open_circuits:
            self._logger.warning("[%s] All providers are open; retrying by priority order.", self._group)
        return [provider for _, provider in indexed]

    def record_success(self, provider: dict[str, Any]) -> None:
        provider_name = self._provider_name(provider)
        state = self._states.setdefault(provider_name, CircuitState())
        had_failures = state.consecutive_failures > 0 or state.opened_until > 0
        state.consecutive_failures = 0
        state.opened_until = 0.0
        state.last_error = ""
        state.last_success_at = time.time()
        if had_failures:
            self._logger.info("[%s] Provider=%s recovered and circuit is closed.", self._group, provider_name)

    def record_failure(
        self,
        provider: dict[str, Any],
        error: str,
        *,
        failure_threshold: int,
        cooldown_seconds: int,
    ) -> None:
        provider_name = self._provider_name(provider)
        state = self._states.setdefault(provider_name, CircuitState())
        state.consecutive_failures += 1
        state.last_error = error
        state.last_failure_at = time.time()

        if state.consecutive_failures >= max(1, failure_threshold):
            state.opened_until = time.time() + max(0, cooldown_seconds)
            self._logger.warning(
                "[%s] Provider=%s opened circuit for %ss after %s consecutive failures. error=%s",
                self._group,
                provider_name,
                max(0, cooldown_seconds),
                state.consecutive_failures,
                error,
            )
            return

        self._logger.warning(
            "[%s] Provider=%s failed %s/%s. error=%s",
            self._group,
            provider_name,
            state.consecutive_failures,
            max(1, failure_threshold),
            error,
        )

    @staticmethod
    def _provider_name(provider: dict[str, Any], index: int | None = None) -> str:
        if provider.get("name"):
            return str(provider["name"])
        if index is not None:
            return f"provider-{index + 1}"
        return "provider"
