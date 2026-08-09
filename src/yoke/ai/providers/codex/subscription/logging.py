"""Codex subscription provider implementation."""

# ruff: noqa: ANN401,C901,D101,D102,D103,E501,S105

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path


def log_provider_event(
    logs_dir: Path, provider_name: str, event: str, **fields: object
) -> None:
    try:
        resolved_logs_dir = logs_dir.expanduser()
        resolved_logs_dir.mkdir(parents=True, exist_ok=True)
        now = datetime.now(UTC)
        log_path = resolved_logs_dir / f"{provider_name}-{now:%Y-%m-%d}.jsonl"
        payload = {
            "ts": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "provider": provider_name,
            "event": event,
            "pid": os.getpid(),
            "thread_id": threading.get_ident(),
            **sanitize_log_fields(fields),
        }
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")))
            handle.write("\n")
    except Exception:
        return


def sanitize_log_fields(fields: dict[str, object]) -> dict[str, object]:
    return {
        key: sanitize_log_value(value)
        for key, value in fields.items()
        if value is not None
    }


def sanitize_log_value(value: object) -> object:
    if isinstance(value, str):
        return value if len(value) <= 300 else f"{value[:297]}..."
    if isinstance(value, int | float | bool):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return sanitize_log_fields(value)  # ty: ignore[invalid-argument-type]
    if isinstance(value, list | tuple):
        return [sanitize_log_value(item) for item in value[:20]]
    return str(value)


def exception_summary(exc: Exception) -> str:
    message = str(exc).strip()
    if len(message) > 220:
        message = f"{message[:217]}..."
    if not message:
        return exc.__class__.__name__
    return f"{exc.__class__.__name__}: {message}"
