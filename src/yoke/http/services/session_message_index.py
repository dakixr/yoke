"""Persistent byte-offset index for paginated HTTP transcript reads."""

from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Lock

from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.http.services.session_message_index_models import ContextIndexWindow
from yoke.http.services.session_message_index_models import MessageIndexSnapshot
from yoke.http.services.session_message_index_models import MessagePage
from yoke.http.services.session_message_index_models import NavigationIndexPreview
from yoke.http.services.session_message_index_models import RuntimeIndexSeed
from yoke.http.services.session_message_index_models import TreeIndexPage
from yoke.http.services.session_message_index_paths import active_ids
from yoke.http.services.session_message_index_paths import ascending_ids
from yoke.http.services.session_message_index_paths import descending_ids
from yoke.http.services.session_message_index_paths import path_to
from yoke.http.services.session_message_index_queries import query_context_window
from yoke.http.services.session_message_index_queries import query_entry
from yoke.http.services.session_message_index_queries import query_entry_tree_state
from yoke.http.services.session_message_index_queries import query_navigation_preview
from yoke.http.services.session_message_index_queries import query_navigation_target
from yoke.http.services.session_message_index_queries import query_page
from yoke.http.services.session_message_index_queries import query_runtime_seed
from yoke.http.services.session_message_index_queries import query_tool_trace_messages
from yoke.http.services.session_message_index_queries import query_tree_page
from yoke.http.services.session_message_index_sidecar import load_sidecar
from yoke.http.services.session_message_index_sidecar import sidecar_path
from yoke.http.services.session_message_index_sidecar import write_sidecar
from yoke.http.services.session_message_index_storage import cached
from yoke.http.services.session_message_index_storage import clone_sidecar
from yoke.http.services.session_message_index_storage import close
from yoke.http.services.session_message_index_storage import current_snapshot
from yoke.http.services.session_message_index_storage import ensure
from yoke.http.services.session_message_index_storage import read_entries
from yoke.http.services.session_message_index_storage import scan
from yoke.http.services.session_message_index_storage import session_lock
from yoke.http.services.session_message_index_storage import store_cache
from yoke.http.services.session_message_index_storage import warm_async
from yoke.http.services.session_message_index_storage import warm_one
from yoke.http.services.session_message_index_tail import has_persisted_tool_entries
from yoke.http.services.session_message_index_tail import tail_context_window
from yoke.http.services.session_message_index_tail import tail_tree_page
from yoke.http.services.session_message_index_tail_navigation import indexed_leaf_id
from yoke.http.services.session_message_index_tail_navigation import tail_entry
from yoke.http.services.session_message_index_tail_navigation import (
    tail_navigation_preview,
)
from yoke.http.services.session_message_index_tail_navigation import tail_page
from yoke.session import SessionStore


class SessionMessageIndex:
    """Index session topology without decoding historical message bodies."""

    def __init__(self, store: SessionStore, *, max_cached_sessions: int = 8) -> None:
        self.store = store
        self.max_cached_sessions = max_cached_sessions
        self._cache_lock = Lock()
        self._session_locks: dict[str, Lock] = {}
        self._cache: OrderedDict[str, MessageIndexSnapshot] = OrderedDict()
        self._warming: set[str] = set()
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="yoke-http-read-index",
        )

    def page(
        self,
        session_id: str,
        *,
        limit: int,
        order: str,
        anchor_id: str | None,
    ) -> MessagePage | None:
        return query_page(
            self,
            session_id,
            limit=limit,
            order=order,
            anchor_id=anchor_id,
        )

    def entry(self, session_id: str, entry_id: str) -> ConversationEntry | None:
        return query_entry(self, session_id, entry_id)

    def tool_trace_messages(self, session_id: str) -> list[Message] | None:
        return query_tool_trace_messages(self, session_id)

    def tree_page(
        self,
        session_id: str,
        *,
        limit: int,
        anchor_id: str | None,
    ) -> TreeIndexPage | None:
        return query_tree_page(self, session_id, limit=limit, anchor_id=anchor_id)

    def context_window(
        self,
        session_id: str,
        *,
        limit: int,
        include_instructions: bool,
    ) -> ContextIndexWindow | None:
        return query_context_window(
            self,
            session_id,
            limit=limit,
            include_instructions=include_instructions,
        )

    def runtime_seed(self, session_id: str) -> RuntimeIndexSeed | None:
        return query_runtime_seed(self, session_id)

    def navigation_preview(
        self,
        session_id: str,
        *,
        target_id: str,
        abandoned_limit: int,
    ) -> NavigationIndexPreview | None:
        return query_navigation_preview(
            self,
            session_id,
            target_id=target_id,
            abandoned_limit=abandoned_limit,
        )

    def entry_tree_state(
        self,
        session_id: str,
        entry_id: str,
    ) -> tuple[ConversationEntry, bool, bool, int] | None:
        return query_entry_tree_state(self, session_id, entry_id)

    def navigation_target(
        self,
        session_id: str,
        target_id: str,
    ) -> ConversationEntry | None:
        return query_navigation_target(self, session_id, target_id)

    def clone_sidecar(self, source_session_id: str, target_session_id: str) -> None:
        clone_sidecar(self, source_session_id, target_session_id)

    def warm_async(self, session_id: str) -> None:
        warm_async(self, session_id)

    def close(self) -> None:
        close(self)

    def _warm_one(self, session_id: str) -> None:
        warm_one(self, session_id)

    def _current_snapshot(self, session_id: str) -> MessageIndexSnapshot | None:
        return current_snapshot(self, session_id)

    def _has_persisted_tool_entries(self, session_id: str) -> bool | None:
        return has_persisted_tool_entries(self, session_id)

    def _tail_tree_page(self, session_id: str, *, limit: int) -> TreeIndexPage | None:
        return tail_tree_page(self, session_id, limit=limit)

    def _tail_context_window(
        self,
        session_id: str,
        *,
        limit: int,
        include_instructions: bool,
    ) -> ContextIndexWindow | None:
        return tail_context_window(
            self,
            session_id,
            limit=limit,
            include_instructions=include_instructions,
        )

    def _tail_navigation_preview(
        self,
        session_id: str,
        *,
        target_id: str,
        abandoned_limit: int,
    ) -> NavigationIndexPreview | None:
        return tail_navigation_preview(
            self,
            session_id,
            target_id=target_id,
            abandoned_limit=abandoned_limit,
        )

    def _tail_entry(self, session_id: str, entry_id: str) -> ConversationEntry | None:
        return tail_entry(self, session_id, entry_id)

    def _tail_page(
        self,
        session_id: str,
        *,
        limit: int,
        anchor_id: str | None,
    ) -> MessagePage | None:
        return tail_page(self, session_id, limit=limit, anchor_id=anchor_id)

    def _indexed_leaf_id(
        self,
        session_id: str,
        source_size: int,
        source_mtime_ns: int,
    ) -> str | None:
        return indexed_leaf_id(self, session_id, source_size, source_mtime_ns)

    def _ensure(self, session_id: str) -> MessageIndexSnapshot | None:
        return ensure(self, session_id)

    def _scan(
        self,
        source: Path,
        *,
        start: int,
        source_size: int,
        source_mtime_ns: int,
        prefix_hash: str,
        prior: MessageIndexSnapshot | None,
    ) -> MessageIndexSnapshot | None:
        return scan(
            self,
            source,
            start=start,
            source_size=source_size,
            source_mtime_ns=source_mtime_ns,
            prefix_hash=prefix_hash,
            prior=prior,
        )

    def _descending_ids(
        self,
        snapshot: MessageIndexSnapshot,
        *,
        limit: int,
        anchor_id: str | None,
    ) -> tuple[list[str], bool] | None:
        return descending_ids(self, snapshot, limit=limit, anchor_id=anchor_id)

    def _ascending_ids(
        self,
        snapshot: MessageIndexSnapshot,
        *,
        limit: int,
        anchor_id: str | None,
    ) -> tuple[list[str], bool] | None:
        return ascending_ids(self, snapshot, limit=limit, anchor_id=anchor_id)

    @staticmethod
    def _active_ids(snapshot: MessageIndexSnapshot) -> list[str] | None:
        return active_ids(snapshot)

    @staticmethod
    def _path_to(
        snapshot: MessageIndexSnapshot,
        entry_id: str,
    ) -> list[str] | None:
        return path_to(snapshot, entry_id)

    def _read_entries(
        self,
        session_id: str,
        snapshot: MessageIndexSnapshot,
        entry_ids: list[str],
    ) -> list[ConversationEntry] | None:
        return read_entries(self, session_id, snapshot, entry_ids)

    def _cached(self, session_id: str) -> MessageIndexSnapshot | None:
        return cached(self, session_id)

    def _store_cache(self, session_id: str, snapshot: MessageIndexSnapshot) -> None:
        store_cache(self, session_id, snapshot)

    def _session_lock(self, session_id: str) -> Lock:
        return session_lock(self, session_id)

    def _sidecar_path(self, session_id: str) -> Path:
        return sidecar_path(self, session_id)

    def _load_sidecar(self, session_id: str) -> MessageIndexSnapshot | None:
        return load_sidecar(self, session_id)

    def _write_sidecar(self, session_id: str, snapshot: MessageIndexSnapshot) -> None:
        write_sidecar(self, session_id, snapshot)
