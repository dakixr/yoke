"""Project persisted and runtime tool traces through the public HTTP contract."""

from __future__ import annotations

import json
from collections import OrderedDict
from threading import Lock
from typing import cast
from typing import Literal
from typing import Protocol
from weakref import WeakValueDictionary

from yoke.agent.observability import ToolTraceEntry
from yoke.agent.observability import ToolTraceStore
from yoke.agent.observability.tool_transcript import entries_from_messages
from yoke.agent.observability.tool_transcript import merge_trace_entries
from yoke.http.errors import ApiError
from yoke.http.models.common import CursorInfo
from yoke.http.models.tool_trace import ToolCallInfo
from yoke.http.models.tool_trace import ToolCallListResponse
from yoke.http.models.tool_trace import ToolCallResponse
from yoke.http.models.tool_trace import ToolOutputChunk
from yoke.http.models.tool_trace import ToolOutputCursor
from yoke.http.models.tool_trace import ToolOutputResponse
from yoke.http.models.tool_trace import ToolTraceArguments
from yoke.http.models.tool_trace import ToolTraceContextInfo
from yoke.http.models.tool_trace import ToolTraceOutputInfo
from yoke.http.models.tool_trace import ToolTraceTime
from yoke.http.services.cursor import decode_cursor
from yoke.http.services.cursor import encode_cursor
from yoke.http.services.cursor import query_fingerprint
from yoke.http.services.redaction import redact_public_value
from yoke.http.services.session_message_index import SessionMessageIndex
from yoke.http.services.session_read_cache import SessionReadCache
from yoke.session import SessionStore


MAX_PERSISTED_TRACE_CACHE_SESSIONS = 1


class ToolTraceRuntime(Protocol):
    """Runtime surface required by tool-trace projection."""

    def tool_trace_store(self) -> ToolTraceStore: ...

    def latest_turn_id(self) -> int: ...


class ToolTraceRuntimeRegistry(Protocol):
    """Registry surface required by tool-trace projection."""

    def get_if_loaded(self, session_id: str) -> ToolTraceRuntime | None: ...


class ToolTraceService:
    """Merge durable conversation tool calls with richer live runtime traces."""

    def __init__(
        self,
        store: SessionStore,
        registry: ToolTraceRuntimeRegistry,
        read_cache: SessionReadCache | None = None,
        message_index: SessionMessageIndex | None = None,
    ) -> None:
        self.store = store
        self.registry = registry
        self.read_cache = read_cache or SessionReadCache(store)
        self.message_index = message_index or SessionMessageIndex(store)
        self._persisted_cache_lock = Lock()
        self._persisted_cache: OrderedDict[
            str,
            tuple[
                tuple[int, int, int],
                tuple[ToolTraceEntry, ...],
                dict[str, ToolTraceEntry],
            ],
        ] = OrderedDict()
        self._persisted_cache_session_locks: WeakValueDictionary[str, Lock] = (
            WeakValueDictionary()
        )

    def list_calls(
        self,
        session_id: str,
        *,
        status: str | None,
        turn_id: int | None,
        limit: int,
        cursor: str | None,
    ) -> ToolCallListResponse:
        entries, live_ids = self._entries(session_id, turn_id=turn_id)
        if status is not None:
            entries = [entry for entry in entries if entry.status == status]
        if turn_id is not None:
            entries = [entry for entry in entries if entry.turn_id == turn_id]
        fingerprint = query_fingerprint((session_id, status, turn_id))
        start = 0
        if cursor is not None:
            anchor = decode_cursor(cursor, expected_query=fingerprint)
            for index, entry in enumerate(entries):
                if entry.tool_call_id == anchor:
                    start = index + 1
                    break
            else:
                raise ApiError(
                    400, "invalid_cursor_anchor", "Cursor anchor no longer exists."
                )
        page = entries[start : start + limit]
        next_cursor = None
        if start + limit < len(entries) and page:
            next_cursor = encode_cursor(
                query=fingerprint,
                anchor_id=page[-1].tool_call_id,
            )
        return ToolCallListResponse(
            data=[
                self._project(session_id, entry, entry.tool_call_id in live_ids)
                for entry in page
            ],
            cursor=CursorInfo(previous=None, next=next_cursor),
        )

    def call(self, session_id: str, call_id: str) -> ToolCallResponse:
        self._require_exists(session_id)
        runtime = self.registry.get_if_loaded(session_id)
        if runtime is not None:
            live = runtime.tool_trace_store().get(call_id)
            if live is not None:
                return ToolCallResponse(data=self._project(session_id, live, True))
        entry = self._persisted_entry(session_id, call_id)
        if entry is None:
            raise ApiError(404, "tool_call_not_found", "Tool call was not found.")
        return ToolCallResponse(data=self._project(session_id, entry, False))

    def output(
        self,
        session_id: str,
        call_id: str,
        *,
        after_seq: int,
        limit: int,
    ) -> ToolOutputResponse:
        self._require_exists(session_id)
        runtime = self.registry.get_if_loaded(session_id)
        if runtime is None:
            return _empty_output()
        store = runtime.tool_trace_store()
        if store.get(call_id) is None:
            if self._persisted_entry(session_id, call_id) is not None:
                return _empty_output()
            raise ApiError(404, "tool_call_not_found", "Tool call was not found.")
        page = store.output_page(call_id, after_seq=after_seq, limit=limit)
        return ToolOutputResponse(
            data=[
                ToolOutputChunk(seq=chunk.seq, stream=chunk.stream, text=chunk.text)
                for chunk in page.chunks
            ],
            cursor=ToolOutputCursor(
                next=page.latest_seq,
                truncated_before=page.truncated_before_seq,
            ),
        )

    def _entries(
        self,
        session_id: str,
        *,
        turn_id: int | None = None,
    ) -> tuple[list[ToolTraceEntry], set[str]]:
        self._require_exists(session_id)
        runtime = self.registry.get_if_loaded(session_id)
        live = runtime.tool_trace_store().snapshot() if runtime is not None else []
        if (
            turn_id is not None
            and runtime is not None
            and runtime.latest_turn_id() == turn_id
        ):
            current = [entry for entry in live if entry.turn_id == turn_id]
            return current, {entry.tool_call_id for entry in current}
        completed = self._persisted_entries(session_id)
        return merge_trace_entries(completed, live), {
            entry.tool_call_id for entry in live
        }

    def _persisted_entries(self, session_id: str) -> list[ToolTraceEntry]:
        entries, _by_id = self._persisted_trace_snapshot(session_id)
        return list(entries)

    def _persisted_entry(
        self,
        session_id: str,
        call_id: str,
    ) -> ToolTraceEntry | None:
        _entries, by_id = self._persisted_trace_snapshot(session_id)
        return by_id.get(call_id)

    def _persisted_trace_snapshot(
        self,
        session_id: str,
    ) -> tuple[tuple[ToolTraceEntry, ...], dict[str, ToolTraceEntry]]:
        fingerprint = self._session_fingerprint(session_id)
        cached = self._cached_persisted_trace(session_id, fingerprint)
        if cached is not None:
            return cached

        session_lock = self._persisted_cache_session_lock(session_id)
        with session_lock:
            fingerprint = self._session_fingerprint(session_id)
            cached = self._cached_persisted_trace(session_id, fingerprint)
            if cached is not None:
                return cached

            messages = self.message_index.tool_trace_messages(session_id)
            parsed = entries_from_messages(
                messages
                if messages is not None
                else self._require_session(session_id).messages
            )
            entries = tuple(parsed)
            by_id = {entry.tool_call_id: entry for entry in entries}
            if fingerprint == self._session_fingerprint(session_id):
                with self._persisted_cache_lock:
                    self._persisted_cache[session_id] = (fingerprint, entries, by_id)
                    self._persisted_cache.move_to_end(session_id)
                    while (
                        len(self._persisted_cache) > MAX_PERSISTED_TRACE_CACHE_SESSIONS
                    ):
                        self._persisted_cache.popitem(last=False)
            return entries, by_id

    def _cached_persisted_trace(
        self,
        session_id: str,
        fingerprint: tuple[int, int, int],
    ) -> tuple[tuple[ToolTraceEntry, ...], dict[str, ToolTraceEntry]] | None:
        with self._persisted_cache_lock:
            cached = self._persisted_cache.get(session_id)
            if cached is None or cached[0] != fingerprint:
                return None
            self._persisted_cache.move_to_end(session_id)
            return cached[1], cached[2]

    def _persisted_cache_session_lock(self, session_id: str) -> Lock:
        with self._persisted_cache_lock:
            return self._persisted_cache_session_locks.setdefault(session_id, Lock())

    def _session_fingerprint(self, session_id: str) -> tuple[int, int, int]:
        path = self.store.directory / f"{session_id}.jsonl"
        try:
            stat = path.stat()
        except OSError as exc:
            raise ApiError(404, "session_not_found", "Session was not found.") from exc
        return stat.st_ino, stat.st_size, stat.st_mtime_ns

    def _project(
        self,
        session_id: str,
        entry: ToolTraceEntry,
        live: bool,
    ) -> ToolCallInfo:
        latest_seq = 0
        truncated = False
        if live:
            runtime = self.registry.get_if_loaded(session_id)
            if runtime is not None:
                output_page = runtime.tool_trace_store().output_page(
                    entry.tool_call_id,
                    after_seq=0,
                    limit=0,
                )
                latest_seq = output_page.latest_seq
                truncated = output_page.truncated_before_seq > 0
        result = redact_public_value(entry.result)
        executed = redact_public_value(entry.executed_arguments)
        public_result = (
            cast(dict[str, object], result) if isinstance(result, dict) else None
        )
        public_executed = (
            cast(dict[str, object], executed) if isinstance(executed, dict) else None
        )
        return ToolCallInfo(
            id=entry.tool_call_id,
            tool_name=entry.tool_name,
            status=_status(entry.status),
            iteration=entry.iteration,
            turn_id=entry.turn_id,
            arguments=ToolTraceArguments(
                raw=_redact_raw_arguments(entry.raw_arguments),
                executed=public_executed,
            ),
            time=ToolTraceTime(
                started=entry.started_wall_at,
                ended=entry.ended_wall_at,
                duration_ms=(
                    round(entry.duration_seconds * 1000)
                    if entry.duration_seconds is not None
                    else None
                ),
            ),
            result=public_result,
            output=ToolTraceOutputInfo(
                retained_chars=sum(
                    len(chunk.text) for chunk in entry.output_chunks or []
                ),
                truncated=truncated,
                latest_seq=latest_seq,
            ),
            context=[
                ToolTraceContextInfo(role=item.role, text=item.text)
                for item in entry.context or []
            ],
            after_context=[
                ToolTraceContextInfo(role=item.role, text=item.text)
                for item in entry.after_context or []
            ],
            retention="runtime" if live else "session",
        )

    def _require_session(self, session_id: str):  # noqa: ANN202
        self._require_exists(session_id)
        return self.read_cache.get(session_id).record

    def _require_exists(self, session_id: str) -> None:
        if not self.store.exists(session_id):
            raise ApiError(404, "session_not_found", "Session was not found.")


def _status(
    value: str,
) -> Literal["pending", "running", "ok", "failed", "cancelled"]:
    if value in {"pending", "running", "ok", "failed", "cancelled"}:
        return cast(
            Literal["pending", "running", "ok", "failed", "cancelled"],
            value,
        )
    return "failed"


def _redact_raw_arguments(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value
    redacted = redact_public_value(parsed)
    return json.dumps(redacted, ensure_ascii=False, separators=(",", ":"))


def _empty_output() -> ToolOutputResponse:
    return ToolOutputResponse(
        data=[],
        cursor=ToolOutputCursor(next=0, truncated_before=0),
    )
