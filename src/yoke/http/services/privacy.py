"""Redact client-only values before daemon persistence and observability."""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from weakref import WeakSet
from yoke.environment import observation_redaction
from threading import RLock

from yoke.agent.models import ConversationEntry


class SessionPrivacy(logging.Filter):
    """Own memory-only value redactions for every attached environment.

    Keep old values until daemon shutdown: queued and retired workers can still
    produce output after their client detaches or replaces its environment.
    """

    def __init__(self) -> None:
        super().__init__()
        self._values: set[str] = set()
        self._lock = RLock()
        self._handlers: WeakSet[logging.Handler] = WeakSet()

    def register(self, values: Mapping[str, str]) -> None:
        with self._lock:
            self._values.update(
                value
                for name, value in values.items()
                if value
                and name != "YOKE_HEADLESS"
                and (
                    os.environ.get(name) != value
                    or any(
                        part in name.lower()
                        for part in ("key", "token", "secret", "password")
                    )
                )
            )
        # Handler filters cover descendant loggers, including provider failures.
        for logger in (
            logging.getLogger(),
            *logging.Logger.manager.loggerDict.values(),
        ):
            if isinstance(logger, logging.Logger):
                for handler in logger.handlers:
                    if self not in handler.filters:
                        handler.addFilter(self)
                        self._handlers.add(handler)

    def scope(self):
        """Bind sanitation for provider file logging during daemon work."""
        return observation_redaction(self.value)

    def close(self) -> None:
        """Release handler references when an application shuts down."""
        for handler in list(self._handlers):
            handler.removeFilter(self)
        self._handlers.clear()

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

    def entries(self, entries: list[ConversationEntry]) -> list[ConversationEntry]:
        with self._lock:
            if not self._values:
                return entries
        return [
            ConversationEntry.model_validate(self.value(entry.model_dump(mode="json")))
            for entry in entries
        ]

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.text(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = self.text(
                logging.Formatter().formatException(record.exc_info)
            )
        return True
