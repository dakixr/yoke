"""Cursor anchoring and revision values shared by session projections."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from yoke.http.errors import ApiError
from yoke.http.services.cursor import decode_cursor


class Identified(Protocol):
    @property
    def id(self) -> str: ...


def cursor_start(
    records: Sequence[Identified], cursor: str | None, fingerprint: str
) -> int:
    if cursor is None:
        return 0
    return anchor_start(records, decode_cursor(cursor, expected_query=fingerprint))


def anchor_start(records: Sequence[Identified], anchor: str | None) -> int:
    if anchor is None:
        return 0
    for index, record in enumerate(records):
        if record.id == anchor:
            return index + 1
    raise ApiError(400, "invalid_cursor_anchor", "Cursor anchor no longer exists.")


def revision_from_timestamp(value: str | None) -> int:
    if not value:
        return 0
    try:
        return int(datetime.fromisoformat(value).timestamp() * 1_000_000)
    except ValueError:
        return 0
