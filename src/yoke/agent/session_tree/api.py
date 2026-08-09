"""SessionTree aggregate and intent-level mutation interface."""

from __future__ import annotations

from collections.abc import Sequence
import secrets
from typing import overload

from yoke.agent.models import ConversationEntry
from yoke.agent.models import MemorySnapshot
from yoke.agent.models import Message

from .errors import ForeignEntryError
from .errors import UnknownEntryError
from .projections import AuditProjection
from .projections import AuditView
from .projections import ConversationProjection
from .projections import ConversationView
from .projections import ProviderProjection
from .projections import ProviderView
from .projections import RuntimeProjection
from .projections import RuntimeContextSeed
from .projections import RuntimeView
from .projections import ScrollbackProjection
from .projections import ScrollbackView
from .values import EntryRef
from .values import TreeExport
from ._merge import reconcile_entries
from ._merge import reconcile_legacy_entries_by_message
from ._mutations import SessionTreeMutations
from ._navigation import SessionTreeNavigation
from ._projection import audit_view
from ._projection import conversation_view
from ._memory import parse_memory_message
from ._projection import provider_view
from ._projection import runtime_view
from ._projection import take_runtime_context
from ._projection import scrollback_view
from ._topology import active_path
from ._topology import copy_and_validate
from ._topology import filter_imported_transcript
from ._topology import repair_legacy


class SessionTree(SessionTreeNavigation, SessionTreeMutations):
    """Own session topology, selection, reconciliation, and projections."""

    def __init__(
        self,
        entries: list[ConversationEntry],
        leaf_id: str | None,
        *,
        scope: str | None = None,
        legacy_message_reconciliation: bool = False,
    ) -> None:
        self._entries = entries
        self._leaf_id = leaf_id
        self._scope = scope or secrets.token_hex(16)
        self._legacy_message_reconciliation = legacy_message_reconciliation

    @classmethod
    def restore(
        cls,
        entries: Sequence[ConversationEntry] | None,
        leaf_id: str | None = None,
        assume_linear: bool = False,
    ) -> SessionTree:
        """Restore and validate a persistence-compatible entry tree."""
        copied, current = copy_and_validate(
            entries, leaf_id, assume_linear=assume_linear
        )
        return cls(copied, current)

    @classmethod
    def borrow_validated(
        cls,
        entries: Sequence[ConversationEntry],
        leaf_id: str | None,
    ) -> SessionTree:
        """Borrow entry values after an owner has validated their topology."""
        return cls(list(entries), leaf_id)

    @classmethod
    def take_validated_runtime(
        cls,
        entries: list[ConversationEntry],
        leaf_id: str | None = None,
    ) -> RuntimeContextSeed:
        """Take an owned, validated active path into a runtime seed."""
        return take_runtime_context(entries, leaf_id)

    @classmethod
    def from_messages(cls, messages: Sequence[Message] | None) -> SessionTree:
        """Create a linear tree from legacy transcript messages."""
        tree = cls([], None)
        for message in messages or ():
            if not isinstance(message, Message):
                raise TypeError("Session messages must be Message values.")
            if message.role == "system":
                tree._append_entry("instruction", message=message)
            else:
                tree._append_imported_message(message)
        return tree

    @classmethod
    def import_legacy(
        cls,
        entries: Sequence[ConversationEntry] | None,
        leaf_id: str | None = None,
        *,
        assume_linear: bool = False,
    ) -> SessionTree:
        """Repair topology and enable legacy transcript reconciliation."""
        repaired, current = repair_legacy(
            entries,
            leaf_id,
            assume_linear=assume_linear,
        )
        return cls(repaired, current, legacy_message_reconciliation=True)

    @classmethod
    def import_filtered_transcript(
        cls,
        entries: Sequence[ConversationEntry],
        retained_messages: Sequence[Message],
    ) -> SessionTree:
        """Import filtered messages and reconnect retained descendants."""
        filtered, current = filter_imported_transcript(entries, retained_messages)
        return cls(filtered, current)

    @classmethod
    def from_runtime_messages(
        cls,
        messages: Sequence[Message],
        snapshot: MemorySnapshot | None,
    ) -> SessionTree:
        """Import a legacy runtime transcript and its memory marker."""
        marker_present = any(
            message.role in {"system", "user"}
            and parse_memory_message(message.plain_text_content or "") is not None
            for message in messages
        )
        tree = cls([], None)
        if snapshot is not None and not marker_present:
            tree.append_snapshot(snapshot)
        for message in messages:
            parsed = parse_memory_message(message.plain_text_content or "")
            if message.role in {"system", "user"} and parsed is not None:
                if snapshot is not None:
                    tree.append_snapshot(snapshot)
                continue
            if message.role == "system":
                continue
            tree._append_imported_message(message)
        return tree

    @classmethod
    def restore_runtime(
        cls,
        entries: Sequence[ConversationEntry],
        fallback_snapshot: MemorySnapshot | None = None,
    ) -> SessionTree:
        """Restore runtime entries while removing instruction topology."""
        instruction_parents = {
            entry.id: entry.parent_id
            for entry in entries
            if entry.kind == "instruction"
        }
        runtime_entries: list[ConversationEntry] = []
        for source in entries:
            if source.kind == "instruction":
                continue
            entry = source.model_copy(deep=True)
            while entry.parent_id in instruction_parents:
                entry.parent_id = instruction_parents[entry.parent_id]
            runtime_entries.append(entry)
        if fallback_snapshot is not None and not any(
            entry.kind == "memory_snapshot" for entry in runtime_entries
        ):
            snapshot = cls._new_entry(
                "memory_snapshot",
                parent_id=None,
                metadata=fallback_snapshot.model_dump(),
            )
            for entry in runtime_entries:
                if entry.parent_id is None:
                    entry.parent_id = snapshot.id
            runtime_entries.insert(0, snapshot)
        return cls.restore(runtime_entries)

    @property
    def current(self) -> EntryRef | None:
        """Return the opaque current entry reference."""
        leaf_id = self._leaf_id
        if leaf_id is None:
            return None
        return EntryRef(self._scope, leaf_id)

    @property
    def entries(self) -> tuple[ConversationEntry, ...]:
        """Return defensive raw entries for persistence compatibility only."""
        return tuple(entry.model_copy(deep=True) for entry in self._entries)

    @property
    def leaf_id(self) -> str | None:
        """Return the raw current identifier for persistence compatibility."""
        return self._leaf_id

    def export_for_persistence(self) -> TreeExport:
        """Export defensive persistence-compatible entries and current leaf."""
        return TreeExport(entries=self.entries, leaf_id=self._leaf_id)

    def export_active_for_persistence(self) -> TreeExport:
        """Export defensive active entries for a legacy runtime adapter."""
        entries = tuple(
            entry.model_copy(deep=True)
            for entry in active_path(self._entries, self._leaf_id)
        )
        return TreeExport(entries=entries, leaf_id=self._leaf_id)

    def export_append_delta(self, start_index: int) -> TreeExport:
        """Export defensive entries appended after a persistence cursor."""
        if start_index < 0 or start_index > len(self._entries):
            raise ValueError("Persistence cursor is outside the session tree.")
        return TreeExport(
            entries=tuple(
                entry.model_copy(deep=True) for entry in self._entries[start_index:]
            ),
            leaf_id=self._leaf_id,
        )

    def export_entry_for_persistence(self, entry_id: str) -> ConversationEntry:
        """Export one defensive entry after a copy-on-write mutation."""
        entry = next(item for item in self._entries if item.id == entry_id)
        return entry.model_copy(deep=True)

    def export(self) -> TreeExport:
        """Export a defensive persistence snapshot for compatibility."""
        return self.export_for_persistence()

    def ref_from_persisted_id(self, entry_id: str) -> EntryRef:
        """Resolve one raw persisted identifier at an adapter ingress."""
        if not isinstance(entry_id, str) or not any(
            entry.id == entry_id for entry in self._entries
        ):
            raise UnknownEntryError(
                "Persisted entry identifier is not in this session tree."
            )
        return EntryRef(self._scope, entry_id)

    def checkout(self, target: EntryRef) -> EntryRef:
        """Select an existing entry so the next append creates a branch."""
        entry_id = self._resolve_ref(target)
        self._leaf_id = entry_id
        return EntryRef(self._scope, entry_id)

    def reconcile(
        self,
        entries: Sequence[ConversationEntry],
        *,
        leaf_id: str | None = None,
    ) -> EntryRef | None:
        """Accept a branch by shared entry identity and select it atomically."""
        if self._legacy_message_reconciliation:
            return self.reconcile_legacy_import(entries, leaf_id=leaf_id)
        incoming, incoming_leaf = copy_and_validate(
            entries, leaf_id, assume_linear=False
        )
        if not incoming:
            return self.current
        validated, selected = reconcile_entries(
            self._entries,
            incoming,
            incoming_leaf,
        )
        self._entries = validated
        self._leaf_id = selected
        return self.current

    def reconcile_legacy_import(
        self,
        entries: Sequence[ConversationEntry],
        *,
        leaf_id: str | None = None,
    ) -> EntryRef | None:
        """Reconcile identity-free imported history by message equality."""
        incoming, incoming_leaf = copy_and_validate(
            entries, leaf_id, assume_linear=False
        )
        if not incoming:
            return self.current
        return self._reconcile_legacy_copies(incoming, incoming_leaf)

    def _reconcile_legacy_copies(
        self,
        incoming: list[ConversationEntry],
        incoming_leaf: str | None,
    ) -> EntryRef | None:
        validated, selected = reconcile_legacy_entries_by_message(
            self._entries,
            self._leaf_id,
            incoming,
            incoming_leaf,
        )
        self._entries = validated
        self._leaf_id = selected
        return self.current

    @overload
    def project(self, spec: RuntimeProjection) -> RuntimeView: ...

    @overload
    def project(self, spec: ProviderProjection) -> ProviderView: ...

    @overload
    def project(self, spec: ScrollbackProjection) -> ScrollbackView: ...

    @overload
    def project(self, spec: AuditProjection) -> AuditView: ...

    @overload
    def project(self, spec: ConversationProjection) -> ConversationView: ...

    def project(
        self,
        spec: RuntimeProjection
        | ProviderProjection
        | ScrollbackProjection
        | AuditProjection
        | ConversationProjection,
    ) -> RuntimeView | ProviderView | ScrollbackView | AuditView | ConversationView:
        """Build one immutable typed projection from the current tree."""
        if isinstance(spec, RuntimeProjection):
            return runtime_view(self._entries, self._leaf_id, self._scope)
        if isinstance(spec, ProviderProjection):
            return provider_view(self._entries, self._leaf_id)
        if isinstance(spec, ScrollbackProjection):
            return scrollback_view(self._entries, self._leaf_id, limit=spec.limit)
        if isinstance(spec, AuditProjection):
            return audit_view(self._entries, self._leaf_id, self._scope)
        if isinstance(spec, ConversationProjection):
            return conversation_view(self._entries, self._leaf_id)
        raise TypeError(f"Unsupported projection specification: {type(spec)!r}")

    def _resolve_ref(self, target: EntryRef) -> str:
        if not isinstance(target, EntryRef) or not target._belongs_to(self._scope):
            raise ForeignEntryError(
                "Entry reference belongs to a different session tree."
            )
        entry_id = target._entry_key()
        if not any(entry.id == entry_id for entry in self._entries):
            raise UnknownEntryError("Entry reference is not in this session tree.")
        return entry_id
