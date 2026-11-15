"""Shared repository calculations."""

from __future__ import annotations

from collections.abc import Sequence

from yoke.agent.models import ConversationEntry
from yoke.session.repository.utils import parse_timestamp


def last_user_message_at(
    current: str | None, entries: Sequence[ConversationEntry]
) -> str | None:
    latest = current
    latest_at = parse_timestamp(current)
    for entry in entries:
        if entry.kind != "user":
            continue
        created_at = parse_timestamp(entry.created_at)
        if created_at is None:
            continue
        if latest_at is None or created_at > latest_at:
            latest = entry.created_at
            latest_at = created_at
    return latest
