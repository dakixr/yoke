"""Session repository adapter used by HTTP routes."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Literal

from yoke.agent.session_tree import SessionTree
from yoke.agent.session_tree.projections import ConversationProjection
from yoke.http.errors import ApiError
from yoke.http.models.common import CursorInfo
from yoke.http.models.common import LocationInfo
from yoke.http.models.session import MessageListResponse
from yoke.http.models.session import MessageResponse
from yoke.http.models.session import ContextData
from yoke.http.models.session import ContextMessage
from yoke.http.models.session import ContextResponse
from yoke.http.models.session import SessionCreateRequest
from yoke.http.models.session import SessionForkRequest
from yoke.http.models.session import SessionInfo
from yoke.http.models.session import SessionListResponse
from yoke.http.models.session import SessionQueueSummary
from yoke.http.models.session import SessionSelection
from yoke.http.models.session import SessionTime
from yoke.http.models.session import SessionTreeSummary
from yoke.http.models.session import TreeData
from yoke.http.models.session import TreeEntryPatchData
from yoke.http.models.session import TreeEntryPatchResponse
from yoke.http.models.session import TreeNavigateData
from yoke.http.models.session import TreeNavigateRequest
from yoke.http.models.session import TreeNavigateResponse
from yoke.http.models.session import TreeNavigationAbandonedEntry
from yoke.http.models.session import TreeNavigationPreviewData
from yoke.http.models.session import TreeNavigationPreviewResponse
from yoke.http.models.session import TreeResponse
from yoke.http.services.cursor import decode_cursor
from yoke.http.services.cursor import encode_cursor
from yoke.http.services.cursor import query_fingerprint
from yoke.http.services.event_broker import EventService
from yoke.http.services.projectors import project_active_messages
from yoke.http.services.projectors import project_entry
from yoke.http.services.projectors import project_tree
from yoke.http.services.projectors import project_message_content
from yoke.session import SessionRecord
from yoke.session import SessionStore
from yoke.session import SessionTreeIndex
from yoke.session import fork_session_title
from yoke.session import new_unique_session_id
from yoke.session.queue import load_prompt_queue_snapshot


SessionOrder = Literal[
    "updatedDesc",
    "updatedAsc",
    "createdDesc",
    "createdAsc",
]
MessageOrder = Literal["asc", "desc"]


class SessionService:
    """Thin application service over the existing session repository."""

    def __init__(
        self,
        store: SessionStore | None = None,
        events: EventService | None = None,
    ) -> None:
        self.store = store or SessionStore()
        self.events = events

    def list_sessions(
        self,
        *,
        directory: str | None,
        search: str | None,
        pinned: bool | None,
        limit: int,
        order: SessionOrder,
        cursor: str | None,
    ) -> SessionListResponse:
        records = [self.store.load(item.id) for item in self.store.list(root=directory)]
        if search:
            needle = search.casefold()
            records = [
                record
                for record in records
                if needle in record.id.casefold()
                or needle in (record.title or "").casefold()
            ]
        if pinned is not None:
            records = [record for record in records if record.pinned is pinned]
        records.sort(key=lambda record: self._sort_key(record, order), reverse=order.endswith("Desc"))

        fingerprint = query_fingerprint((directory, search, pinned, order))
        start = self._cursor_start(records, cursor, fingerprint)
        page = records[start : start + limit]
        next_cursor = None
        if start + limit < len(records) and page:
            next_cursor = encode_cursor(query=fingerprint, anchor_id=page[-1].id)
        return SessionListResponse(
            data=[self.session_info(record) for record in page],
            cursor=CursorInfo(previous=None, next=next_cursor),
        )

    def get_session(self, session_id: str) -> SessionInfo:
        return self.session_info(self._require_record(session_id))

    def create_session(self, request: SessionCreateRequest) -> SessionInfo:
        root = str(Path(request.location.directory).resolve())
        session_id = request.id or new_unique_session_id(self.store.exists)
        if self.store.exists(session_id):
            existing = self.store.load(session_id)
            if existing.root is not None and str(Path(existing.root).resolve()) != root:
                raise ApiError(
                    409,
                    "session_identity_conflict",
                    "Session id already exists at another location.",
                    {"sessionID": session_id, "location": existing.root},
                )
            return self.session_info(existing)
        selection = request.selection
        record = self.store.save(
            session_id,
            [],
            root=root,
            title=request.title,
            provider_name=selection.provider if selection is not None else None,
            model_id=selection.model if selection is not None else None,
            reasoning_effort=(
                selection.reasoning_effort if selection is not None else None
            ),
        )
        self._publish(
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
    ) -> SessionInfo:
        if not title_set and not pinned_set:
            raise ApiError(400, "empty_mutation", "At least one field is required.")
        record = self._require_record(session_id)
        if title_set:
            record = self.store.set_title(session_id, title, existing_record=record)
        if pinned_set:
            if pinned is None:
                raise ApiError(400, "invalid_pinned", "pinned cannot be null.")
            record = self.store.set_pinned(session_id, pinned, existing_record=record)
        self._publish(
            record,
            "session.updated",
            {
                "sessionID": record.id,
                "title": record.title,
                "pinned": record.pinned,
            },
        )
        return self.session_info(record)

    def fork_session(self, session_id: str, request: SessionForkRequest) -> SessionInfo:
        source = self._require_record(session_id)
        fork_id = request.id or new_unique_session_id(self.store.exists)
        if self.store.exists(fork_id):
            raise ApiError(409, "session_identity_conflict", "Fork id already exists.")
        if request.from_entry_id is None:
            forked = self.store.fork(
                session_id,
                new_session_id_value=fork_id,
                title=request.title,
            )
            self._publish(
                forked,
                "session.created",
                {"sessionID": forked.id, "sourceSessionID": session_id},
            )
            return self.session_info(forked)
        tree = SessionTree.restore(source.conversation_entries, source.leaf_id)
        try:
            target = tree.ref_from_persisted_id(request.from_entry_id)
        except ValueError as exc:
            raise ApiError(404, "entry_not_found", "Tree entry was not found.") from exc
        tree.checkout(target)
        exported = tree.export_for_persistence()
        transcript = tree.project(ConversationProjection()).transcript_messages
        forked = self.store.save(
            fork_id,
            list(transcript),
            conversation_entries=list(exported.entries),
            leaf_id=exported.leaf_id,
            active_skills=source.active_skills,
            skill_dirs=source.skill_dirs,
            root=source.root,
            title=request.title or fork_session_title(source.title),
            provider_name=source.provider_name,
            model_id=source.model_id,
            reasoning_effort=source.reasoning_effort,
            context_window_tokens=source.context_window_tokens,
        )
        self._publish(
            forked,
            "session.created",
            {
                "sessionID": forked.id,
                "sourceSessionID": session_id,
                "fromEntryID": request.from_entry_id,
            },
        )
        return self.session_info(forked)

    def messages(
        self,
        session_id: str,
        *,
        limit: int,
        order: MessageOrder,
        cursor: str | None,
    ) -> MessageListResponse:
        record = self._require_record(session_id)
        messages = project_active_messages(record.conversation_entries, record.leaf_id)
        if order == "desc":
            messages.reverse()
        fingerprint = query_fingerprint((session_id, "active", order))
        start = self._message_cursor_start(messages, cursor, fingerprint)
        page = messages[start : start + limit]
        next_cursor = None
        if start + limit < len(messages) and page:
            next_cursor = encode_cursor(query=fingerprint, anchor_id=page[-1].id)
        return MessageListResponse(
            data=page,
            cursor=CursorInfo(previous=None, next=next_cursor),
        )

    def message(self, session_id: str, message_id: str) -> MessageResponse:
        record = self._require_record(session_id)
        for entry in record.conversation_entries:
            if entry.id == message_id and entry.kind not in {"instruction", "memory_snapshot"}:
                return MessageResponse(data=project_entry(entry))
        raise ApiError(404, "message_not_found", "Message was not found.")

    def context(
        self,
        session_id: str,
        *,
        include_system: bool,
        include_tool_results: bool,
    ) -> ContextResponse:
        """Return the current model-visible conversation projection."""
        record = self._require_record(session_id)
        view = SessionTree.restore(
            record.conversation_entries,
            record.leaf_id,
        ).project(ConversationProjection())
        source_messages = list(view.provider_messages)
        if include_system:
            instructions = [
                entry.message
                for entry in view.active_entries
                if entry.kind == "instruction" and entry.message is not None
            ]
            source_messages = [*instructions, *source_messages]
        messages = []
        for message in source_messages:
            if message.role == "system" and not include_system:
                continue
            if message.role == "tool" and not include_tool_results:
                continue
            messages.append(
                ContextMessage(
                    role=message.role,
                    content=project_message_content(message),
                    tool_call_id=message.tool_call_id,
                    phase=message.phase,
                )
            )
        return ContextResponse(data=ContextData(messages=messages))

    def tree(self, session_id: str) -> TreeResponse:
        record = self._require_record(session_id)
        return TreeResponse(
            data=TreeData(
                revision=_revision_from_timestamp(record.updated_at),
                leaf_id=record.leaf_id,
                entries=project_tree(record.conversation_entries, record.leaf_id),
            )
        )

    def navigation_preview(
        self,
        session_id: str,
        *,
        target_id: str,
        include_abandoned: bool,
    ) -> TreeNavigationPreviewResponse:
        record = self._require_record(session_id)
        tree = SessionTree.restore(record.conversation_entries, record.leaf_id)
        try:
            target = tree.ref_from_persisted_id(target_id)
            preview = tree.preview_navigation(
                target,
                include_abandoned=include_abandoned,
            )
        except ValueError as exc:
            raise ApiError(404, "entry_not_found", "Tree entry was not found.") from exc
        abandoned: list[TreeNavigationAbandonedEntry] = []
        for item in preview.abandoned:
            message = item.message.to_message() if item.message is not None else None
            text = message.display_text_content() if message is not None else item.summary_text
            abandoned.append(
                TreeNavigationAbandonedEntry(
                    id=item.ref._entry_key(),
                    kind=item.kind,
                    preview=_preview_text(text),
                )
            )
        return TreeNavigationPreviewResponse(
            data=TreeNavigationPreviewData(
                target_id=target_id,
                current=preview.current,
                editor_text=preview.editor_text,
                abandoned=abandoned,
            )
        )

    def navigate_tree(
        self,
        session_id: str,
        request: TreeNavigateRequest,
    ) -> TreeNavigateResponse:
        record = self._require_record(session_id)
        self._require_tree_revision(record, request.expected_revision)
        tree = SessionTree.restore(record.conversation_entries, record.leaf_id)
        try:
            target = tree.ref_from_persisted_id(request.target_id)
            outcome = tree.navigate(target, branch_summary=request.branch_summary)
        except ValueError as exc:
            raise ApiError(404, "entry_not_found", "Tree entry was not found.") from exc
        exported = tree.export_for_persistence()
        if exported.leaf_id is None:
            raise ApiError(
                409,
                "tree_root_navigation_unsupported",
                "This legacy tree has no persisted parent before the selected user entry.",
            )
        appended = list(exported.entries[len(record.conversation_entries) :])
        index = SessionTreeIndex(record.conversation_entries, record.leaf_id)
        updated = self.store.save_tree_delta(
            session_id,
            existing_record=record,
            tree_index=index,
            leaf_id=exported.leaf_id,
            appended_entries=appended,
        )
        revision = _revision_from_timestamp(updated.updated_at)
        self._publish(
            updated,
            "session.tree.updated",
            {
                "revision": revision,
                "leafID": updated.leaf_id,
                "targetID": request.target_id,
            },
        )
        return TreeNavigateResponse(
            data=TreeNavigateData(
                revision=revision,
                leaf_id=updated.leaf_id,
                editor_text=outcome.editor_text,
                summary_added=outcome.summary_appended,
            )
        )

    def set_tree_label(
        self,
        session_id: str,
        entry_id: str,
        *,
        expected_revision: int,
        label: str | None,
    ) -> TreeEntryPatchResponse:
        record = self._require_record(session_id)
        self._require_tree_revision(record, expected_revision)
        tree = SessionTree.restore(record.conversation_entries, record.leaf_id)
        try:
            target = tree.ref_from_persisted_id(entry_id)
            tree.set_label(target, label)
            entry = tree.export_entry_for_persistence(entry_id)
        except ValueError as exc:
            raise ApiError(404, "entry_not_found", "Tree entry was not found.") from exc
        index = SessionTreeIndex(record.conversation_entries, record.leaf_id)
        updated = self.store.save_entry_metadata(
            session_id,
            entry,
            existing_record=record,
            tree_index=index,
        )
        revision = _revision_from_timestamp(updated.updated_at)
        self._publish(
            updated,
            "session.tree.updated",
            {"revision": revision, "entryID": entry_id},
        )
        projected_entry = next(
            item
            for item in project_tree(updated.conversation_entries, updated.leaf_id)
            if item.id == entry_id
        )
        return TreeEntryPatchResponse(
            data=TreeEntryPatchData(
                revision=revision,
                entry=projected_entry,
            )
        )

    def session_info(self, record: SessionRecord) -> SessionInfo:
        queue = load_prompt_queue_snapshot(self.store.directory, record.id)
        steering = sum(item.kind == "steering" for item in queue.prompts)
        paused = sum(item.paused for item in queue.prompts)
        return SessionInfo(
            id=record.id,
            title=record.title,
            pinned=record.pinned,
            location=LocationInfo(directory=record.root or ""),
            time=SessionTime(created=record.created_at, updated=record.updated_at),
            selection=SessionSelection(
                provider=record.provider_name,
                model=record.model_id,
                reasoning_effort=record.reasoning_effort,
            ),
            tree=SessionTreeSummary(
                leaf_id=record.leaf_id,
                entry_count=len(record.conversation_entries),
            ),
            queue=SessionQueueSummary(
                total=len(queue.prompts),
                steering=steering,
                queued=len(queue.prompts) - steering,
                paused=paused,
                revision=queue.revision,
            ),
        )

    def _require_record(self, session_id: str) -> SessionRecord:
        if not self.store.exists(session_id):
            raise ApiError(404, "session_not_found", "Session was not found.")
        return self.store.load(session_id)

    def _publish(
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

    @staticmethod
    def _require_tree_revision(record: SessionRecord, expected: int) -> None:
        actual = _revision_from_timestamp(record.updated_at)
        if actual != expected:
            raise ApiError(
                409,
                "tree_revision_conflict",
                f"Tree changed since revision {expected}.",
                {"expectedRevision": expected, "actualRevision": actual},
            )

    @staticmethod
    def _sort_key(record: SessionRecord, order: SessionOrder) -> tuple[str, str]:
        value = (
            record.created_at
            if order.startswith("created")
            else record.updated_at or record.created_at
        )
        return value or "", record.id

    @staticmethod
    def _cursor_start(
        records: list[SessionRecord], cursor: str | None, fingerprint: str
    ) -> int:
        if cursor is None:
            return 0
        anchor = decode_cursor(cursor, expected_query=fingerprint)
        for index, record in enumerate(records):
            if record.id == anchor:
                return index + 1
        raise ApiError(400, "invalid_cursor_anchor", "Cursor anchor no longer exists.")

    @staticmethod
    def _message_cursor_start(
        messages: Sequence[object], cursor: str | None, fingerprint: str
    ) -> int:
        if cursor is None:
            return 0
        anchor = decode_cursor(cursor, expected_query=fingerprint)
        for index, message in enumerate(messages):
            if getattr(message, "id", None) == anchor:
                return index + 1
        raise ApiError(400, "invalid_cursor_anchor", "Cursor anchor no longer exists.")


def _revision_from_timestamp(value: str | None) -> int:
    if not value:
        return 0
    try:
        return int(datetime.fromisoformat(value).timestamp() * 1_000_000)
    except ValueError:
        return 0


def _preview_text(value: str | None, limit: int = 160) -> str | None:
    if not value:
        return None
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."
