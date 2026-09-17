"""Opaque model-facing cursors over internal retained process output positions."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import struct


CURSOR_PREFIX = "pc1_"
_CURSOR_STRUCT = struct.Struct(">QQQ")
CURSOR_ENCODED_LENGTH = 32
CURSOR_LENGTH = len(CURSOR_PREFIX) + CURSOR_ENCODED_LENGTH
CURSOR_PATTERN = rf"^{CURSOR_PREFIX}[A-Za-z0-9_-]{{{CURSOR_ENCODED_LENGTH}}}$"


@dataclass(slots=True, frozen=True)
class ProcessPosition:
    """Internal retained-output position for one process session."""

    session_id: int
    after_seq: int = 0
    offset: int = 0


def encode_cursor(position: ProcessPosition) -> str:
    """Encode an internal position as one versioned opaque cursor."""
    raw = _CURSOR_STRUCT.pack(
        position.session_id,
        position.after_seq,
        position.offset,
    )
    encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{CURSOR_PREFIX}{encoded}"


def decode_cursor(session_id: int, cursor: str | None) -> ProcessPosition:
    """Resolve a public cursor and verify that it belongs to the target session."""
    if cursor is None:
        return ProcessPosition(session_id=session_id)
    if not is_cursor_token(cursor):
        raise ValueError("Invalid process cursor")
    encoded = cursor[len(CURSOR_PREFIX) :]
    try:
        raw = base64.urlsafe_b64decode(encoded)
        cursor_session_id, after_seq, offset = _CURSOR_STRUCT.unpack(raw)
    except (ValueError, struct.error) as exc:
        raise ValueError("Invalid process cursor") from exc
    if cursor_session_id != session_id:
        raise ValueError("Process cursor belongs to a different session")
    return ProcessPosition(
        session_id=session_id,
        after_seq=after_seq,
        offset=offset,
    )


def is_cursor_token(value: object) -> bool:
    """Return whether a value has the public process-cursor wire shape."""
    return (
        isinstance(value, str)
        and value.startswith(CURSOR_PREFIX)
        and len(value) == CURSOR_LENGTH
        and all(
            character.isalnum() or character in "-_"
            for character in value[len(CURSOR_PREFIX) :]
        )
    )
