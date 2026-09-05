"""Extracted helpers for the persistent HTTP session message index."""

from __future__ import annotations

from yoke.http.services.session_message_index.models import MessageIndexSnapshot
from yoke.http.services.session_message_index.models import PUBLIC_EXCLUDED_KINDS
from yoke.http.services.session_message_index.models import kind
from yoke.http.services.session_message_index.models import parent_id


def descending_ids(
    snapshot: MessageIndexSnapshot,
    *,
    limit: int,
    anchor_id: str | None,
) -> tuple[list[str], bool] | None:
    current = snapshot.leaf_id
    if anchor_id is not None:
        found = False
        while current is not None:
            if current == anchor_id:
                found = True
                location = snapshot.entries.get(current)
                current = parent_id(location) if location is not None else None
                break
            location = snapshot.entries.get(current)
            if location is None:
                return None
            current = parent_id(location)
        if not found:
            return None
    selected: list[str] = []
    while current is not None and len(selected) <= limit:
        location = snapshot.entries.get(current)
        if location is None:
            return None
        if kind(location) not in PUBLIC_EXCLUDED_KINDS:
            selected.append(current)
        current = parent_id(location)
    return selected[:limit], len(selected) > limit


def ascending_ids(
    snapshot: MessageIndexSnapshot,
    *,
    limit: int,
    anchor_id: str | None,
) -> tuple[list[str], bool] | None:
    active: list[str] = []
    current = snapshot.leaf_id
    while current is not None:
        location = snapshot.entries.get(current)
        if location is None:
            return None
        if kind(location) not in PUBLIC_EXCLUDED_KINDS:
            active.append(current)
        current = parent_id(location)
    active.reverse()
    start = 0
    if anchor_id is not None:
        try:
            start = active.index(anchor_id) + 1
        except ValueError:
            return None
    page = active[start : start + limit]
    return page, start + limit < len(active)


def active_ids(snapshot: MessageIndexSnapshot) -> list[str] | None:
    active: list[str] = []
    current = snapshot.leaf_id
    seen: set[str] = set()
    while current is not None:
        if current in seen:
            return None
        seen.add(current)
        location = snapshot.entries.get(current)
        if location is None:
            return None
        active.append(current)
        current = parent_id(location)
    active.reverse()
    return active


def path_to(
    snapshot: MessageIndexSnapshot,
    entry_id: str,
) -> list[str] | None:
    reverse_path: list[str] = []
    current: str | None = entry_id
    seen: set[str] = set()
    while current is not None:
        if current in seen:
            return None
        seen.add(current)
        location = snapshot.entries.get(current)
        if location is None:
            return None
        reverse_path.append(current)
        current = parent_id(location)
    reverse_path.reverse()
    return reverse_path
