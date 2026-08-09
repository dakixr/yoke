"""Validated retained topology index for one active CLI session."""

from __future__ import annotations

from collections.abc import Sequence

from yoke.agent.models import ConversationEntry
from yoke.agent.session_tree import DuplicateEntryError
from yoke.agent.session_tree import ForwardParentError
from yoke.agent.session_tree import InvalidCurrentError
from yoke.agent.session_tree import MissingParentError
from yoke.agent.session_tree import ParentCycleError


class SessionTreeIndex:
    """Retain validated entry and parent indexes for an active session."""

    def __init__(
        self,
        entries: Sequence[ConversationEntry],
        leaf_id: str | None,
    ) -> None:
        self._entries = entries
        self._by_id: dict[str, ConversationEntry] = {}
        self._parent_by_id: dict[str, str | None] = {}
        self._positions: dict[str, int] = {}
        self._leaf_id = leaf_id
        self._active_refs: tuple[ConversationEntry, ...] | None = None
        self._validate_complete_tree()

    @property
    def leaf_id(self) -> str | None:
        """Return the selected persisted leaf."""
        return self._leaf_id

    def active_entry_refs(self) -> tuple[ConversationEntry, ...]:
        """Return borrowed active-path values from root to leaf."""
        if self._active_refs is None:
            reverse_path: list[ConversationEntry] = []
            current = self._leaf_id
            while current is not None:
                reverse_path.append(self._by_id[current])
                current = self._parent_by_id[current]
            reverse_path.reverse()
            self._active_refs = tuple(reverse_path)
        return self._active_refs

    def active_entries(self) -> list[ConversationEntry]:
        """Return defensive copies of active-path values."""
        return [entry.model_copy(deep=True) for entry in self.active_entry_refs()]

    def entry_ref(self, entry_id: str) -> ConversationEntry:
        """Return one borrowed entry by its validated persisted ID."""
        try:
            return self._by_id[entry_id]
        except KeyError:
            raise ValueError("Entry is not in the active session index.") from None

    def entry_position(self, entry_id: str) -> int:
        """Return one entry's stable event-order position."""
        try:
            return self._positions[entry_id]
        except KeyError:
            raise ValueError("Entry is not in the active session index.") from None

    def prove_active_append(
        self,
        incoming: Sequence[ConversationEntry],
        leaf_id: str | None,
    ) -> tuple[ConversationEntry, ...] | None:
        """Return a validated suffix when incoming extends the active path."""
        active = self.active_entry_refs()
        if len(incoming) < len(active):
            return None
        if any(
            entry is not incoming[index] and entry != incoming[index]
            for index, entry in enumerate(active)
        ):
            return None
        suffix = tuple(incoming[len(active) :])
        selected = leaf_id or (incoming[-1].id if incoming else self._leaf_id)
        if not suffix:
            return () if selected == self._leaf_id else None
        parent_id = self._leaf_id
        for entry in suffix:
            if not isinstance(entry, ConversationEntry) or (
                entry.parent_id != parent_id
            ):
                return None
            parent_id = entry.id
        if selected != suffix[-1].id:
            return None
        return self.prove_append(suffix, selected)

    def prove_append(
        self,
        entries: Sequence[ConversationEntry],
        leaf_id: str | None,
    ) -> tuple[ConversationEntry, ...] | None:
        """Return a suffix whose IDs, parents, and selected leaf are valid."""
        suffix = tuple(entries)
        new_ids: set[str] = set()
        for entry in suffix:
            if not isinstance(entry, ConversationEntry):
                return None
            if not entry.id or entry.id in self._by_id or entry.id in new_ids:
                return None
            parent_id = entry.parent_id
            if (
                parent_id is not None
                and parent_id not in self._by_id
                and parent_id not in new_ids
            ):
                return None
            new_ids.add(entry.id)
        if leaf_id is None or (leaf_id not in self._by_id and leaf_id not in new_ids):
            return None
        return suffix

    def commit_append(
        self,
        entries: Sequence[ConversationEntry],
        leaf_id: str | None,
        suffix: Sequence[ConversationEntry],
    ) -> None:
        """Advance indexes after the writer commits a proven suffix."""
        position = len(self._positions)
        for entry in suffix:
            self._by_id[entry.id] = entry
            self._parent_by_id[entry.id] = entry.parent_id
            self._positions[entry.id] = position
            position += 1
        self._entries = entries
        self._leaf_id = leaf_id
        self._active_refs = None

    def replace(
        self,
        entries: Sequence[ConversationEntry],
        leaf_id: str | None,
    ) -> None:
        """Replace all retained topology after a non-append reconciliation."""
        replacement = type(self)(entries, leaf_id)
        self._entries = replacement._entries
        self._by_id = replacement._by_id
        self._parent_by_id = replacement._parent_by_id
        self._positions = replacement._positions
        self._leaf_id = replacement._leaf_id
        self._active_refs = replacement._active_refs

    def commit_entry_replacement(
        self,
        entries: Sequence[ConversationEntry],
        entry: ConversationEntry,
    ) -> None:
        """Replace one indexed value after an in-place event is committed."""
        existing = self.entry_ref(entry.id)
        if existing.parent_id != entry.parent_id:
            raise ValueError("Entry metadata updates cannot change topology.")
        self._entries = entries
        self._by_id[entry.id] = entry
        self._parent_by_id[entry.id] = entry.parent_id
        self._active_refs = None

    def _validate_complete_tree(self) -> None:
        for position, entry in enumerate(self._entries):
            if not isinstance(entry, ConversationEntry):
                raise TypeError(
                    "Session tree entries must be ConversationEntry values."
                )
            if not entry.id or entry.id in self._by_id:
                token = entry.id[:8] if entry.id else "<empty>"
                raise DuplicateEntryError(
                    f"Session tree contains duplicate entry id {token!r}."
                )
            self._by_id[entry.id] = entry
            self._parent_by_id[entry.id] = entry.parent_id
            self._positions[entry.id] = position
        for entry in self._entries:
            parent_id = entry.parent_id
            if parent_id is not None and parent_id not in self._by_id:
                raise MissingParentError(
                    f"Entry {entry.id[:8]!r} references a missing parent."
                )
            if parent_id is not None and (
                self._positions[parent_id] > self._positions[entry.id]
            ):
                raise ForwardParentError(
                    f"Entry {entry.id[:8]!r} references a parent that occurs "
                    "later in event order."
                )
        self._validate_cycles()
        if not self._entries:
            if self._leaf_id is not None:
                raise InvalidCurrentError("An empty session tree cannot have a leaf.")
            return
        if self._leaf_id is None:
            self._leaf_id = self._entries[-1].id
        if self._leaf_id not in self._by_id:
            raise InvalidCurrentError("The selected session-tree leaf is missing.")

    def _validate_cycles(self) -> None:
        done: set[str] = set()
        for entry_id in self._by_id:
            if entry_id in done:
                continue
            path: set[str] = set()
            current: str | None = entry_id
            while current is not None and current not in done:
                if current in path:
                    raise ParentCycleError("Session tree contains a parent cycle.")
                path.add(current)
                current = self._parent_by_id[current]
            done.update(path)
