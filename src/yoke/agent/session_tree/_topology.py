"""Topology validation and iterative traversal."""

from __future__ import annotations

from collections.abc import Sequence
import secrets

from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message

from .errors import DuplicateEntryError
from .errors import ForwardParentError
from .errors import InvalidCurrentError
from .errors import MissingParentError
from .errors import ParentCycleError


def copy_and_validate(
    entries: Sequence[ConversationEntry] | None,
    leaf_id: str | None,
    *,
    assume_linear: bool,
) -> tuple[list[ConversationEntry], str | None]:
    """Copy persisted entries and validate their complete topology."""
    copied: list[ConversationEntry] = []
    previous: str | None = None
    for source in entries or ():
        if not isinstance(source, ConversationEntry):
            raise TypeError("Session tree entries must be ConversationEntry values.")
        entry = source.model_copy(deep=True)
        if assume_linear and entry.parent_id is None and copied:
            entry.parent_id = previous
        copied.append(entry)
        previous = entry.id
    by_id: dict[str, ConversationEntry] = {}
    for entry in copied:
        if not entry.id or entry.id in by_id:
            token = entry.id[:8] if entry.id else "<empty>"
            raise DuplicateEntryError(
                f"Session tree contains duplicate entry id {token!r}."
            )
        by_id[entry.id] = entry
    positions = {entry.id: index for index, entry in enumerate(copied)}
    for index, entry in enumerate(copied):
        if entry.parent_id is not None and entry.parent_id not in by_id:
            raise MissingParentError(
                f"Entry {entry.id[:8]!r} references a missing parent."
            )
        if entry.parent_id is not None and positions[entry.parent_id] > index:
            raise ForwardParentError(
                f"Entry {entry.id[:8]!r} references a parent that occurs "
                "later in event order."
            )
    _validate_cycles(copied, by_id)
    if not copied:
        if leaf_id is not None:
            raise InvalidCurrentError("An empty session tree cannot have a leaf.")
        return copied, None
    resolved_leaf = leaf_id if leaf_id is not None else copied[-1].id
    if resolved_leaf not in by_id:
        raise InvalidCurrentError("The selected session-tree leaf is missing.")
    return copied, resolved_leaf


def repair_legacy(
    entries: Sequence[ConversationEntry] | None,
    leaf_id: str | None,
    *,
    assume_linear: bool,
) -> tuple[list[ConversationEntry], str | None]:
    """Repair legacy IDs and parent links, then validate the result."""
    repaired: list[ConversationEntry] = []
    seen: set[str] = set()
    previous: str | None = None
    for source in entries or ():
        if not isinstance(source, ConversationEntry):
            raise TypeError("Legacy session entries must be ConversationEntry values.")
        entry = source.model_copy(deep=True)
        if not entry.id or entry.id in seen:
            entry.id = secrets.token_hex(8)
        seen.add(entry.id)
        if entry.parent_id == entry.id:
            entry.parent_id = None
        if assume_linear and entry.parent_id is None and repaired:
            entry.parent_id = previous
        if entry.parent_id is not None and entry.parent_id not in seen:
            entry.parent_id = previous
        repaired.append(entry)
        previous = entry.id
    resolved_leaf = leaf_id if leaf_id in seen else previous
    return copy_and_validate(
        repaired,
        resolved_leaf,
        assume_linear=False,
    )


def filter_imported_transcript(
    entries: Sequence[ConversationEntry],
    retained_messages: Sequence[Message],
) -> tuple[list[ConversationEntry], str | None]:
    """Filter imported messages and reconnect retained descendants."""
    copied, leaf_id = copy_and_validate(entries, None, assume_linear=False)
    retained_iter = iter(retained_messages)
    next_message = next(retained_iter, None)
    parent_mapping: dict[str, str | None] = {}
    filtered: list[ConversationEntry] = []
    for entry in copied:
        parent_id = (
            parent_mapping.get(entry.parent_id) if entry.parent_id is not None else None
        )
        keep = entry.message is None or (
            next_message is not None and entry.message == next_message
        )
        if not keep:
            parent_mapping[entry.id] = parent_id
            continue
        if entry.message is not None:
            if next_message is None:
                raise ValueError("Retained transcript message is missing.")
            entry.message = next_message.model_copy(deep=True)
            next_message = next(retained_iter, None)
        entry.parent_id = parent_id
        filtered.append(entry)
        parent_mapping[entry.id] = entry.id
    selected = parent_mapping.get(leaf_id) if leaf_id is not None else None
    return copy_and_validate(filtered, selected, assume_linear=False)


def _validate_cycles(
    entries: Sequence[ConversationEntry],
    by_id: dict[str, ConversationEntry],
) -> None:
    done: set[str] = set()
    for entry in entries:
        if entry.id in done:
            continue
        path: set[str] = set()
        current: str | None = entry.id
        while current is not None and current not in done:
            if current in path:
                raise ParentCycleError("Session tree contains a parent cycle.")
            path.add(current)
            current = by_id[current].parent_id
        done.update(path)


def active_path(
    entries: Sequence[ConversationEntry], leaf_id: str | None
) -> list[ConversationEntry]:
    """Return the selected root-to-leaf path without recursion."""
    if leaf_id is None:
        return []
    by_id = {entry.id: entry for entry in entries}
    reverse_path: list[ConversationEntry] = []
    current: str | None = leaf_id
    while current is not None:
        entry = by_id[current]
        reverse_path.append(entry)
        current = entry.parent_id
    reverse_path.reverse()
    return reverse_path
