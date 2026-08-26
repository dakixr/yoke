"""Mandatory secret redaction for public HTTP observability payloads."""

from __future__ import annotations

from collections.abc import Mapping
from collections.abc import Sequence


_SENSITIVE_PARTS = (
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "password",
    "private_key",
    "privatekey",
    "secret",
    "token",
)


def redact_public_value(value: object, *, key: str = "") -> object:
    """Return a JSON-compatible value with secret-looking fields removed."""
    normalized = key.casefold().replace("-", "_")
    if normalized and any(part in normalized for part in _SENSITIVE_PARTS):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {
            str(item_key): redact_public_value(item, key=str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [redact_public_value(item) for item in value]
    if value is None or isinstance(value, str | int | float | bool):
        return value
    return str(value)
