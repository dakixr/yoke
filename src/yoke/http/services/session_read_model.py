"""Index-first reads with one signature-consistent legacy fallback."""

from __future__ import annotations

from yoke.agent.models import ConversationEntry
from yoke.agent.session_tree import SessionTree
from yoke.agent.tool_context import legacy_tool_context_entry_ids
from yoke.http.errors import ApiError, session_not_found
from yoke.http.models.session import ContextResponse
from yoke.http.services.projectors import is_public_chat_entry, public_chat_entries
from yoke.http.services.session_cursors import anchor_start
from yoke.http.services.session_message_index import SessionMessageIndex
from yoke.http.services.session_message_index.models import (
    MessagePage,
    NavigationIndexPreview,
    TreeIndexPage,
)
from yoke.http.services.session_read_cache import SessionReadCache, SessionReadSnapshot
from yoke.http.services.session_tree_edit import SessionTreeEdit
from yoke.http.services.session_views import (
    project_indexed_context,
    project_saved_context,
)
from yoke.session import SessionRecord, SessionStore


class SessionReadModel:
    """Hide storage selection from message, context, and tree operations."""

    def __init__(
        self, store: SessionStore, index: SessionMessageIndex, cache: SessionReadCache
    ) -> None:
        self.store = store
        self.index = index
        self.cache = cache

    def snapshot(self, session_id: str) -> SessionReadSnapshot:
        try:
            return self.cache.get(session_id)
        except FileNotFoundError as exc:
            raise session_not_found() from exc

    def record(self, session_id: str) -> SessionRecord:
        return self.snapshot(session_id).record

    def summary(self, session_id: str) -> SessionRecord:
        record = self.store.summary_record(session_id)
        if record is None:
            raise session_not_found()
        return record

    def page(
        self, session_id: str, *, limit: int, order: str, anchor_id: str | None
    ) -> MessagePage:
        indexed = self.index.page(
            session_id, limit=limit, order=order, anchor_id=anchor_id
        )
        if indexed is not None:
            return indexed
        entries = list(
            public_chat_entries(self.snapshot(session_id).active_path_entries)
        )
        if order == "desc":
            entries.reverse()
        start = anchor_start(entries, anchor_id)
        return MessagePage(entries[start : start + limit], start + limit < len(entries))

    def message(self, session_id: str, message_id: str) -> ConversationEntry:
        indexed = self.index.entry(session_id, message_id)
        if indexed is not None:
            return indexed
        snapshot = self.snapshot(session_id)
        entry = snapshot.entries_by_id.get(message_id)
        if entry is not None and is_public_chat_entry(entry):
            if any(
                item.id == entry.id
                for item in public_chat_entries(list(snapshot.entries_by_id.values()))
            ):
                return entry
        raise ApiError(404, "message_not_found", "Message was not found.")

    def context(
        self,
        session_id: str,
        *,
        include_system: bool,
        include_tool_results: bool,
        limit: int,
        max_chars: int,
    ) -> ContextResponse:
        indexed = self.index.context_window(
            session_id, limit=limit, include_instructions=include_system
        )
        if indexed is not None:
            return project_indexed_context(
                indexed,
                include_system=include_system,
                include_tool_results=include_tool_results,
                max_chars=max_chars,
            )
        return project_saved_context(
            self.record(session_id),
            include_system=include_system,
            include_tool_results=include_tool_results,
            limit=limit,
            max_chars=max_chars,
        )

    def tree_page(
        self, session_id: str, *, limit: int, anchor_id: str | None
    ) -> TreeIndexPage:
        indexed = self.index.tree_page(session_id, limit=limit, anchor_id=anchor_id)
        if indexed is not None:
            return indexed
        snapshot = self.snapshot(session_id)
        entries = snapshot.record.conversation_entries
        SessionTree.restore(entries, snapshot.record.leaf_id)
        end = (
            anchor_start(entries, anchor_id) - 1
            if anchor_id is not None
            else len(entries)
        )
        start = max(0, end - limit)
        children: dict[str, int] = {}
        for entry in entries:
            if entry.parent_id is not None:
                children[entry.parent_id] = children.get(entry.parent_id, 0) + 1
        return TreeIndexPage(
            list(entries[start:end]),
            snapshot.record.leaf_id,
            frozenset(entry.id for entry in snapshot.active_path_entries),
            children,
            len(entries),
            start > 0,
            frozenset(legacy_tool_context_entry_ids(entries)),
        )

    def navigation_preview(
        self, session_id: str, *, target_id: str, include_abandoned: bool
    ) -> NavigationIndexPreview:
        indexed = self.index.navigation_preview(
            session_id,
            target_id=target_id,
            abandoned_limit=100 if include_abandoned else 0,
        )
        if indexed is not None:
            return indexed
        snapshot = self.snapshot(session_id)
        tree = SessionTree.restore(
            snapshot.record.conversation_entries, snapshot.record.leaf_id
        )
        try:
            preview = tree.preview_navigation(
                tree.ref_from_persisted_id(target_id),
                include_abandoned=include_abandoned,
            )
        except ValueError as exc:
            raise ApiError(404, "entry_not_found", "Tree entry was not found.") from exc
        # EntryRef remains opaque outside SessionTree. Resolve the abandoned
        # suffix by comparing the validated persisted paths instead.
        validated = {entry.id: entry for entry in tree.entries}
        selected: list[ConversationEntry] = []
        entry = validated[target_id]
        while True:
            selected.append(entry)
            if entry.parent_id is None:
                break
            entry = validated[entry.parent_id]
        selected.reverse()
        common = 0
        for old, new in zip(snapshot.active_path_entries, selected):
            if old.id != new.id:
                break
            common += 1
        abandoned = (
            [validated[entry.id] for entry in snapshot.active_path_entries[common:]]
            if include_abandoned
            else []
        )
        return NavigationIndexPreview(
            validated[target_id],
            preview.current,
            abandoned,
            len(abandoned),
            False,
        )

    def edit(
        self, session_id: str, target_id: str, *, label: bool = False
    ) -> SessionTreeEdit:
        summary = self.summary(session_id)
        state = self.index.entry_tree_state(session_id, target_id) if label else None
        target = (
            (state[0] if state is not None else None)
            if label
            else self.index.navigation_target(session_id, target_id)
        )
        if target is not None:
            return SessionTreeEdit.indexed(self.store, summary, target, state)
        record = self.record(session_id)
        return SessionTreeEdit.legacy(self.store, record, target_id)
