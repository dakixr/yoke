"""Small opaque cursor codec for mutable ordered collections."""

from __future__ import annotations

import base64
import hashlib
import json

from yoke.http.errors import ApiError


def query_fingerprint(parts: tuple[object, ...]) -> str:
    """Return a short stable fingerprint for a pagination query shape."""
    raw = json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def encode_cursor(*, query: str, anchor_id: str) -> str:
    """Encode one opaque collection cursor."""
    raw = json.dumps(
        {"q": query, "id": anchor_id},
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(value: str, *, expected_query: str) -> str:
    """Decode and validate one opaque collection cursor."""
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        payload = json.loads(raw)
        query = payload["q"]
        anchor_id = payload["id"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ApiError(400, "invalid_cursor", "Cursor is invalid.") from exc
    if query != expected_query or not isinstance(anchor_id, str):
        raise ApiError(
            400,
            "cursor_query_mismatch",
            "Cursor does not match this query.",
        )
    return anchor_id

