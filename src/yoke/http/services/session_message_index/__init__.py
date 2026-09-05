"""Persistent byte-offset index for paginated HTTP transcript reads."""

from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.http.services.session_message_index.models import ContextIndexWindow
from yoke.http.services.session_message_index.models import MessageIndexSnapshot
from yoke.http.services.session_message_index.models import MessagePage
from yoke.http.services.session_message_index.models import NavigationIndexPreview
from yoke.http.services.session_message_index.models import RuntimeIndexSeed
from yoke.http.services.session_message_index.models import TreeIndexPage
from yoke.http.services.session_message_index.queries import query_context_window
from yoke.http.services.session_message_index.queries import query_entry
from yoke.http.services.session_message_index.queries import query_navigation_preview
from yoke.http.services.session_message_index.queries import query_page
from yoke.http.services.session_message_index.queries import query_runtime_seed
from yoke.http.services.session_message_index.queries import query_tool_trace_messages
from yoke.http.services.session_message_index.queries import query_tree_page
from yoke.http.services.session_message_index.storage import clone_sidecar
from yoke.http.services.session_message_index.storage import close
from yoke.http.services.session_message_index.storage import ensure
from yoke.http.services.session_message_index.storage import warm_async
from yoke.http.services.session_message_index.tail_navigation import (
    query_entry_tree_state,
)
from yoke.http.services.session_message_index.tail_navigation import (
    query_navigation_target,
)
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

    def _ensure(self, session_id: str) -> MessageIndexSnapshot | None:
        return ensure(self, session_id)
