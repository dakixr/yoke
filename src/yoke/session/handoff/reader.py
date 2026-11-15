"""Bounded active-branch reads for portable session handoffs."""

from __future__ import annotations

from dataclasses import dataclass

from yoke.agent.models import ConversationEntry
from yoke.session.repository.store import SessionStore


@dataclass(frozen=True, slots=True)
class HandoffContextWindow:
    """Bounded active entries plus lightweight source metadata."""

    entries: list[ConversationEntry]
    total_entries: int
    truncated: bool


def read_handoff_context_window(
    store: SessionStore,
    session_id: str,
    *,
    recent_limit: int,
) -> HandoffContextWindow | None:
    """Read the latest checkpoint and recent active tail without full session decode."""
    topology = store.entry_topology(session_id)
    if topology is None or not topology.leaf_id:
        return None
    nodes = {entry_id: (parent, kind) for entry_id, parent, kind in topology.rows}
    recent: list[str] = []
    checkpoint: str | None = None
    current: str | None = topology.leaf_id
    seen: set[str] = set()
    while current is not None and current not in seen:
        node = nodes.get(current)
        if node is None:
            break
        seen.add(current)
        entry_id = current
        current, kind = node
        if kind == "memory_snapshot":
            checkpoint = entry_id
            break
        if len(recent) < recent_limit:
            recent.append(entry_id)

    # A missing parent is not an intentional context bound. Let the canonical
    # loader validate any chain we could not follow to its root or checkpoint.
    if not seen or (current is not None and checkpoint is None):
        return None
    selected = list(reversed(recent))
    if checkpoint is not None:
        selected.insert(0, checkpoint)
    entries = store.read_entries(
        session_id, selected, raw_filter=_elide_embedded_image_data
    )
    if entries is None:
        return None
    return HandoffContextWindow(
        entries=entries,
        total_entries=len(topology.rows),
        truncated=(
            checkpoint is not None or len(seen) > len(recent) or current is not None
        ),
    )


def _elide_embedded_image_data(raw: bytes) -> bytes:
    """Remove inline image bytes before Pydantic parses a selected entry."""
    replacements = (
        (b'"url":"data:', b'"url":"data:"'),
        (b'"data_url":"data:', b'"data_url":null'),
    )
    result = raw
    for marker, replacement in replacements:
        search_from = 0
        parts: list[bytes] | None = None
        while True:
            marker_at = result.find(marker, search_from)
            if marker_at < 0:
                break
            value_start = marker_at + marker.find(b'"data:') + 1
            value_end = result.find(b'"', value_start)
            if value_end < 0:
                break
            if parts is None:
                parts = []
            parts.append(result[search_from:marker_at])
            parts.append(replacement)
            search_from = value_end + 1
        if parts is not None:
            parts.append(result[search_from:])
            result = b"".join(parts)
    return result
