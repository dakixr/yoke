"""Session catalog mutations, list cards, and queue summaries."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal

from yoke.session.repository.models import SessionIndexEntry
from yoke.http.errors import ApiError, session_not_found
from yoke.http.models.common import CursorInfo, LocationInfo
from yoke.http.models.session import (
    SessionCreateRequest,
    SessionInfo,
    SessionListResponse,
    SessionQueueSummary,
    SessionSelection,
    SessionTime,
    SessionTreeSummary,
)
from yoke.http.reads.cursor import decode_cursor, encode_cursor, query_fingerprint
from yoke.http.services.event_broker import EventService
from yoke.http.reads.model import SessionReadModel
from yoke.session import SessionRecord, SessionStore, new_unique_session_id
from yoke.session.queue import (
    PersistedPromptQueue,
    load_prompt_queue_snapshot,
    load_prompt_queue_snapshots,
    prompt_queue_transaction,
)
from yoke.session.workspace import (
    WorkspaceUnavailable,
    inspect_workspace,
    require_workspace,
)

SessionOrder = Literal[
    "updatedDesc",
    "updatedAsc",
    "createdDesc",
    "createdAsc",
    "lastUserDesc",
    "lastUserAsc",
]
MessageOrder = Literal["asc", "desc"]


def _require_no_queued_prompts(queue: PersistedPromptQueue) -> None:
    if queue.prompts:
        raise ApiError(
            409,
            "session_has_pending_work",
            "Session has pending queued prompts. Remove, resume, or let them finish before settling.",
        )


class SessionCatalog:
    """Own metadata, list cards, and durable catalog changes."""

    def __init__(
        self,
        store: SessionStore,
        events: EventService | None,
        read_model: SessionReadModel,
    ) -> None:
        self.store = store
        self.events = events
        self.read_model = read_model

    def list_sessions(
        self,
        *,
        directory: str | None,
        search: str | None,
        pinned: bool | None,
        archived: bool | None,
        limit: int,
        order: SessionOrder,
        cursor: str | None,
    ) -> SessionListResponse:
        fingerprint = query_fingerprint((directory, search, pinned, archived, order))
        anchor = (
            decode_cursor(cursor, expected_query=fingerprint)
            if cursor is not None
            else None
        )
        page, total, has_more = self.read_model.index.queries.sessions(
            directory=directory,
            search=search,
            pinned=pinned,
            archived=archived,
            order=order,
            limit=limit,
            anchor_id=anchor,
        )
        queue_snapshots = load_prompt_queue_snapshots(
            self.store.directory,
            (record.id for record in page),
        )
        next_cursor = None
        if has_more and page:
            next_cursor = encode_cursor(query=fingerprint, anchor_id=page[-1].id)
        return SessionListResponse(
            data=[
                self.session_info(
                    record,
                    queue=self._queue_summary_from_snapshot(queue_snapshots[record.id]),
                )
                for record in page
            ],
            cursor=CursorInfo(previous=None, next=next_cursor),
            total=total,
        )

    def get_session(self, session_id: str) -> SessionInfo:
        if not self.store.exists(session_id):
            raise session_not_found()
        entry = self.store.index_entry(session_id)
        if entry is not None:
            return self.session_info(entry)
        return self.session_info(self.read_model.record(session_id))

    def create_session(self, request: SessionCreateRequest) -> SessionInfo:
        session_id = request.id or new_unique_session_id(self.store.exists)
        if self.store.exists(session_id):
            try:
                root = str(Path(request.location.directory).expanduser().resolve())
            except (OSError, ValueError, RuntimeError) as exc:
                raise WorkspaceUnavailable(request.location.directory) from exc
            existing = self.store.summary_record(session_id)
            if existing is None:
                raise session_not_found()
            if existing.root is not None and str(Path(existing.root).resolve()) != root:
                raise ApiError(
                    409,
                    "session_identity_conflict",
                    "Session id already exists at another location.",
                    {"sessionID": session_id, "location": existing.root},
                )
            entry = self.store.index_entry(session_id)
            return (
                self.session_info(entry)
                if entry is not None
                else self.session_info(existing)
            )
        selection = request.selection
        root = str(require_workspace(request.location.directory))
        record = self.store.save(
            session_id,
            [],
            root=root,
            origin=request.client.kind if request.client else "web",
            title=request.title,
            provider_name=selection.provider if selection is not None else None,
            model_id=selection.model if selection is not None else None,
            reasoning_effort=(
                selection.reasoning_effort if selection is not None else None
            ),
        )
        self.publish(
            record,
            "session.created",
            {"sessionID": record.id},
        )
        return self.session_info(record)

    def patch_session(
        self,
        session_id: str,
        *,
        title_set: bool,
        title: str | None,
        pinned_set: bool,
        pinned: bool | None,
        archived_set: bool,
        archived: bool | None,
        before_mutation: Callable[[], None] | None = None,
    ) -> SessionInfo:
        if not title_set and not pinned_set and not archived_set:
            raise ApiError(400, "empty_mutation", "At least one field is required.")
        record = self.store.summary_record(session_id)
        if record is None:
            raise session_not_found()
        if pinned_set and pinned is None:
            raise ApiError(400, "invalid_pinned", "pinned cannot be null.")
        if archived_set and archived is None:
            raise ApiError(400, "invalid_archived", "archived cannot be null.")
        # Reject an already-busy session before cancelling title generation.
        # This precheck cannot authorize archiving; recheck under the write lock.
        if archived_set and archived:
            _require_no_queued_prompts(
                load_prompt_queue_snapshot(self.store.directory, session_id)
            )
        if before_mutation is not None:
            before_mutation()
        with prompt_queue_transaction(self.store.directory, session_id) as transaction:
            record = self.store.summary_record(session_id)
            if record is None:
                raise session_not_found()
            if archived_set and archived:
                _require_no_queued_prompts(transaction.snapshot)
            if title_set:
                record = self.store.set_title(session_id, title, existing_record=record)
            if pinned_set:
                assert pinned is not None
                record = self.store.set_pinned(
                    session_id, pinned, existing_record=record
                )
            if archived_set:
                assert archived is not None
                record = self.store.set_archived(
                    session_id, archived, existing_record=record
                )
            self.publish(
                record,
                "session.updated",
                {
                    "sessionID": record.id,
                    "title": record.title,
                    "pinned": record.pinned,
                    "archivedAt": record.archived_at,
                },
            )
        entry = self.store.index_entry(session_id)
        return (
            self.session_info(entry) if entry is not None else self.session_info(record)
        )

    def session_info(
        self,
        entry: SessionIndexEntry | SessionRecord,
        *,
        queue: SessionQueueSummary | None = None,
    ) -> SessionInfo:
        """Build a list-card projection without loading conversation history."""
        return SessionInfo(
            id=entry.id,
            title=entry.title,
            pinned=entry.pinned,
            archived_at=entry.archived_at,
            database_path=str(self.store.database_path),
            context_window_tokens=entry.context_window_tokens,
            location=LocationInfo(directory=entry.root or ""),
            workspace=inspect_workspace(entry.root),
            time=SessionTime(
                created=entry.created_at,
                updated=entry.updated_at,
                last_user_message=entry.last_user_message_at,
            ),
            selection=SessionSelection(
                provider=entry.provider_name,
                model=entry.model_id,
                reasoning_effort=entry.reasoning_effort,
            ),
            context_usage=(
                dict(entry.context_usage) if entry.context_usage is not None else None
            ),
            tree=SessionTreeSummary(
                leaf_id=entry.leaf_id,
                entry_count=(entry.entry_count or 0)
                if isinstance(entry, SessionIndexEntry)
                else len(entry.conversation_entries),
            ),
            queue=queue if queue is not None else self._queue_summary(entry.id),
        )

    def _queue_summary(self, session_id: str) -> SessionQueueSummary:
        return self._queue_summary_from_snapshot(
            load_prompt_queue_snapshot(self.store.directory, session_id)
        )

    @staticmethod
    def _queue_summary_from_snapshot(
        queue: PersistedPromptQueue,
    ) -> SessionQueueSummary:
        steering = sum(
            item.kind == "steering" and not item.paused for item in queue.prompts
        )
        queued = sum(
            item.kind == "queued" and not item.paused for item in queue.prompts
        )
        paused = sum(item.paused for item in queue.prompts)
        return SessionQueueSummary(
            total=len(queue.prompts),
            steering=steering,
            queued=queued,
            paused=paused,
            revision=queue.revision,
        )

    def publish(
        self,
        record: SessionRecord,
        event_type: str,
        data: dict[str, object],
    ) -> None:
        if self.events is None:
            return
        self.events.durable(
            record.id,
            event_type,
            data,
            location=record.root,
        )
