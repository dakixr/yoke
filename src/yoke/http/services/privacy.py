"""Memory-only credential sanitation for daemon diagnostics, never transcripts."""

from __future__ import annotations

from collections.abc import Mapping
from threading import RLock

from yoke.ai.providers.credentials import PROVIDER_CREDENTIAL_ENV_NAMES
from yoke.environment import observation_redaction


class SessionPrivacy:
    """Retain secret values until shutdown for queued and retired workers.

    Ordinary environment values, including short terminal dimensions and paths,
    are user data. Only sufficiently long, secret-named values are diagnostic
    redactions. This service must never transform provider context or entries.
    """

    def __init__(self) -> None:
        self._values: set[str] = set()
        self._lock = RLock()

    def register(self, values: Mapping[str, str]) -> None:
        with self._lock:
            self._values.update(
                value
                for name, value in values.items()
                if len(value) >= 8
                and (
                    name.upper() in PROVIDER_CREDENTIAL_ENV_NAMES
                    or any(
                        part in name.casefold()
                        for part in (
                            "key",
                            "token",
                            "secret",
                            "password",
                            "credential",
                            "auth",
                        )
                    )
                )
            )

    def scope(self):
        """Bind sanitation for provider file logging during daemon work."""
        return observation_redaction(self.value)

    def text(self, text: str) -> str:
        with self._lock:
            values = sorted(self._values, key=len, reverse=True)
        for value in values:
            text = text.replace(value, "<redacted>")
        return text

    def value(self, value: object) -> object:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, dict):
            return {key: self.value(item) for key, item in value.items()}
        if isinstance(value, list | tuple):
            return [self.value(item) for item in value]
        return value

    def data(self, value: dict[str, object]) -> dict[str, object]:
        return {key: self.value(item) for key, item in value.items()}

    def trace_data(self, data: dict[str, object]) -> dict[str, object]:
        """Sanitize diagnostic content without changing trace IDs or provenance."""
        fields = {"tool_arguments", "executed_arguments", "result", "text", "error"}
        return {
            key: self.value(value) if key in fields else value
            for key, value in data.items()
        }

    def event_data(self, event_type: str, data: dict[str, object]) -> dict[str, object]:
        """Sanitize diagnostics; preserve transcript-mirroring event content."""
        fields = {"error", "lastError"}
        if event_type in {
            "session.notice",
            "session.warning",
            "session.error",
            "session.runtime.failed",
            "session.provider.retry",
            "session.provider.rate_limited",
            "session.provider.recovered",
            "session.model.started",
            "session.model.ended",
        }:
            fields.update({"message", "text", "content", "reason", "detail", "details"})
        # Identifiers and timing metadata are never subject to substitution.
        # A completion's top-level error is diagnostic; its result is content.
        return {
            key: self.value(value) if key in fields else value
            for key, value in data.items()
        }
