"""Structured artifact helpers for SDK workflows."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from collections.abc import Sequence
from dataclasses import fields
from dataclasses import is_dataclass
from datetime import date
from datetime import datetime
from datetime import time
from enum import Enum
from pathlib import Path

from pydantic import BaseModel

type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
)


def to_jsonable(value: object) -> JsonValue:
    """Recursively normalize common SDK workflow values into JSON data."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, BaseModel):
        return to_jsonable(value.model_dump(mode="json"))
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: to_jsonable(getattr(value, field.name))
            for field in fields(value)
        }
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, Enum):
        return to_jsonable(value.value)
    if isinstance(value, BaseException):
        return {
            "type": type(value).__name__,
            "message": str(value),
        }
    if isinstance(value, Mapping):
        if not all(isinstance(key, str) for key in value):
            raise TypeError("JSON artifact mappings require string keys.")
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray):
        return [to_jsonable(item) for item in value]
    raise TypeError(f"Value is not JSON artifact compatible: {type(value).__name__}")


def write_json_artifact(
    path: str | Path,
    payload: object,
    *,
    atomic: bool = True,
    indent: int = 2,
) -> Path:
    """Write normalized JSON, atomically replacing the target by default."""
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    content = (
        json.dumps(
            to_jsonable(payload),
            ensure_ascii=False,
            indent=indent,
        )
        + "\n"
    )
    if not atomic:
        target.write_text(content, encoding="utf-8")
        return target
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            temporary = Path(handle.name)
        temporary.replace(target)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return target
