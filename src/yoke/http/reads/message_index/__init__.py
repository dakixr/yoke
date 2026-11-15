"""Direct SQL transcript, context, and branch queries.

The historical SessionMessageIndex name remains for runtime integrations. There
is no index state, sidecar, cache or background warming.
"""

from __future__ import annotations

from functools import wraps
from typing import Callable

from yoke.agent.models import ConversationEntry, Message
from yoke.http.reads.message_index.models import (
    ContextIndexWindow,
    MessagePage,
    NavigationIndexPreview,
    RuntimeIndexSeed,
    TreeIndexPage,
)
from yoke.http.reads.queries import SessionQueries
from yoke.session import SessionStore


def _snapshot[**P, R](method: Callable[P, R]) -> Callable[P, R]:
    @wraps(method)
    def read(*args: P.args, **kwargs: P.kwargs) -> R:
        owner = args[0]
        assert isinstance(owner, SessionMessageIndex)
        with owner.store.database.read():
            return method(*args, **kwargs)

    return read


class SessionMessageIndex:
    """Query a consistent SQLite snapshot, decoding only selected entries."""

    def __init__(self, store: SessionStore) -> None:
        self.store = store
        self.queries = SessionQueries(store)

    def ensure(self, session_id: str):  # noqa: ANN201
        """Return session metadata. Kept for runtime integration compatibility."""
        return self.store.summary_record(session_id)

    def warm_async(self, session_id: str) -> None:
        """Compatibility hook; SQL reads need no warming."""

    def close(self) -> None:
        """Compatibility hook; this query facade owns no resources."""

    @_snapshot
    def page(
        self, session_id: str, *, limit: int, order: str, anchor_id: str | None
    ) -> MessagePage | None:
        summary = self.store.summary_record(session_id)
        if summary is None:
            return None
        ids, more = self.queries.page_ids(
            session_id, summary.leaf_id, limit=limit, order=order, anchor_id=anchor_id
        )
        entries = self.queries.read(session_id, ids)
        return None if entries is None else MessagePage(entries, more)

    @_snapshot
    def entry(self, session_id: str, entry_id: str) -> ConversationEntry | None:
        entries = self.queries.read(session_id, [entry_id])
        if entries and entries[0].kind not in {
            "instruction",
            "memory_snapshot",
            "tool_context",
        }:
            return entries[0]
        return None

    @_snapshot
    def tree_page(
        self, session_id: str, *, limit: int, anchor_id: str | None
    ) -> TreeIndexPage | None:
        summary = self.store.index_entry(session_id)
        if summary is None:
            return None
        ids, more = self.queries.page_ids(
            session_id,
            summary.leaf_id,
            limit=limit,
            order="desc",
            anchor_id=anchor_id,
            tree=True,
        )
        ids.reverse()
        entries = self.queries.read(session_id, ids)
        if entries is None:
            return None
        return TreeIndexPage(
            entries,
            summary.leaf_id,
            frozenset(i for i, _ in self.queries.path(session_id, summary.leaf_id)),
            self.queries.children(session_id, ids),
            summary.entry_count or 0,
            more,
        )

    @_snapshot
    def context_window(
        self, session_id: str, *, limit: int, include_instructions: bool
    ) -> ContextIndexWindow | None:
        summary = self.store.summary_record(session_id)
        if summary is None:
            return None
        rows = self.queries.path(session_id, summary.leaf_id)
        ids = [i for i, _ in rows]
        checkpoint = next(
            (
                n
                for n in range(len(rows) - 1, -1, -1)
                if rows[n][1] == "memory_snapshot"
            ),
            None,
        )
        if checkpoint is None:
            selected = ids[-limit:] if limit else []
        else:
            tail = ids[checkpoint + 1 :]
            selected = [ids[checkpoint], *(tail[-limit:] if limit else [])]
        if include_instructions:
            selected = [
                *[i for i, kind in rows if kind == "instruction" and i not in selected],
                *selected,
            ]
        entries = self.queries.read(session_id, selected)
        return (
            None
            if entries is None
            else ContextIndexWindow(entries, len(ids), len(set(selected)) < len(ids))
        )

    @_snapshot
    def tool_trace_messages(self, session_id: str) -> list[Message] | None:
        """Read only active-branch turns that contain persisted tool activity."""
        snapshot = self.store.summary_record(session_id)
        if snapshot is None:
            return None
        rows = self.queries.path(session_id, snapshot.leaf_id)
        active_ids = [entry_id for entry_id, _ in rows]
        kinds = dict(rows)
        if active_ids is None:
            return None
        tool_indexes = [
            index
            for index, entry_id in enumerate(active_ids)
            if kinds[entry_id] in {"assistant_tool_calls", "tool_result"}
        ]
        if not tool_indexes:
            return []

        selected: set[int] = set()
        for index in tool_indexes:
            start = index
            while start > 0:
                previouskind = kinds[active_ids[start - 1]]
                start -= 1
                if previouskind == "user":
                    break
            end = index + 1
            while end < len(active_ids):
                if kinds[active_ids[end]] == "user":
                    break
                end += 1
            selected.update(range(start, end))

        entry_ids = [
            entry_id for index, entry_id in enumerate(active_ids) if index in selected
        ]
        entries = self.queries.read(session_id, entry_ids)
        if entries is None:
            return None
        messages: list[Message] = []
        for entry in entries:
            if entry.message is None:
                continue
            message = entry.message
            if entry.kind == "tool_result":
                projection = entry.metadata.get("provider_result_projection")
                if isinstance(projection, str) and projection:
                    message = message.model_copy(deep=False)
                    message.set_provider_result_projection(projection)
            messages.append(message)
        return messages

    @_snapshot
    def runtime_seed(self, session_id: str) -> RuntimeIndexSeed | None:
        """Read a compacted runtime seed without forcing a cold full index scan."""
        snapshot = self.store.summary_record(session_id)
        if snapshot is None:
            return None
        rows = self.queries.path(session_id, snapshot.leaf_id)
        active_ids = [entry_id for entry_id, _ in rows]
        kinds = dict(rows)
        if not active_ids:
            return None
        checkpoint_index = next(
            (
                index
                for index in range(len(active_ids) - 1, -1, -1)
                if kinds[active_ids[index]] == "memory_snapshot"
            ),
            None,
        )
        if checkpoint_index is None:
            return None

        checkpoint_id = active_ids[checkpoint_index]
        checkpoint_entries = self.queries.read(session_id, [checkpoint_id])
        if not checkpoint_entries:
            return None
        checkpoint = checkpoint_entries[0]
        handoff = checkpoint.metadata.get("compaction_handoff")
        if not isinstance(handoff, dict):
            return None
        retained_messages = handoff.get("retained_messages")
        retained_user_messages = handoff.get("retained_user_messages")
        self_contained = (
            isinstance(retained_messages, list) and bool(retained_messages)
        ) or retained_user_messages == 0
        if not self_contained:
            return None

        cache_scope_id = next(
            (
                entry_id
                for entry_id in active_ids[:checkpoint_index]
                if kinds[entry_id] not in {"instruction", "memory_snapshot"}
            ),
            None,
        )
        prior_skill_ids = [
            entry_id
            for entry_id in active_ids[:checkpoint_index]
            if kinds[entry_id] == "skill_event"
        ]
        continuation_ids = active_ids[checkpoint_index + 1 :]
        prefix_ids = [
            *([cache_scope_id] if cache_scope_id is not None else []),
            *(entry_id for entry_id in prior_skill_ids if entry_id != cache_scope_id),
        ]
        selected_ids = [*prefix_ids, checkpoint_id, *continuation_ids]
        entries = self.queries.read(session_id, selected_ids)
        if not entries:
            return None

        parent_id: str | None = None
        for entry in entries:
            entry.parent_id = parent_id
            parent_id = entry.id
        return RuntimeIndexSeed(
            entries=entries,
            leaf_id=entries[-1].id,
            total_entries=len(active_ids),
        )

    @_snapshot
    def navigation_preview(
        self, session_id: str, *, target_id: str, abandoned_limit: int
    ) -> NavigationIndexPreview | None:
        summary = self.store.summary_record(session_id)
        target = self.navigation_target(session_id, target_id)
        if summary is None or target is None:
            return None
        current = [i for i, _ in self.queries.path(session_id, summary.leaf_id)]
        selected = [i for i, _ in self.queries.path(session_id, target_id)]
        common = 0
        for old, new in zip(current, selected):
            if old != new:
                break
            common += 1
        abandoned = current[common:]
        ids = abandoned[-abandoned_limit:] if abandoned_limit else []
        entries = self.queries.read(session_id, ids)
        if entries is None:
            return None
        return NavigationIndexPreview(
            target,
            target_id == summary.leaf_id,
            entries,
            len(abandoned),
            len(ids) < len(abandoned),
        )

    @_snapshot
    def entry_tree_state(
        self, session_id: str, entry_id: str
    ) -> tuple[ConversationEntry, bool, bool, int] | None:
        summary = self.store.summary_record(session_id)
        entry = self.navigation_target(session_id, entry_id)
        if summary is None or entry is None:
            return None
        active = {i for i, _ in self.queries.path(session_id, summary.leaf_id)}
        return (
            entry,
            entry_id in active,
            entry_id == summary.leaf_id,
            self.queries.children(session_id, [entry_id]).get(entry_id, 0),
        )

    @_snapshot
    def navigation_target(
        self, session_id: str, target_id: str
    ) -> ConversationEntry | None:
        entries = self.queries.read(session_id, [target_id])
        return entries[0] if entries else None
