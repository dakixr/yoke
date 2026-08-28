"""Session repository adapter used by HTTP routes."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Literal

from yoke.agent.models import ConversationEntry
from yoke.agent.models import MemorySnapshot
from yoke.agent.models import Message
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
from yoke.http.services.projectors import project_entry
from yoke.http.services.projectors import project_tree
from yoke.http.services.projectors import project_tree_entry
from yoke.http.services.projectors import project_message_content
from yoke.http.services.session_message_index import SessionMessageIndex
from yoke.http.services.session_read_cache import SessionReadCache
from yoke.http.services.session_read_cache import SessionReadSnapshot
from yoke.session import SessionRecord
from yoke.cli.session.models import SessionIndexEntry
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
        read_cache: SessionReadCache | None = None,
    ) -> None:
        self.store = store or SessionStore()
        self.events = events
        self.message_index = SessionMessageIndex(self.store)
        self.read_cache = read_cache or SessionReadCache(self.store)

    def close(self) -> None:
        """Release background read-index workers."""
        self.message_index.close()

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
        records = self.store.list_index_entries(root=directory, maintain=False)
        if search:
            needle = search.casefold()
            records = [
                record
                for record in records
                if needle in record.id.casefold()
                or needle in (record.title or "").casefold()
                or needle in (record.root or "").casefold()
            ]
        if pinned is not None:
            records = [record for record in records if record.pinned is pinned]
        if archived is not None:
            records = [
                record
                for record in records
                if (record.archived_at is not None) is archived
            ]
        records.sort(
            key=lambda record: self._sort_key(record, order),
            reverse=order.endswith("Desc"),
        )

        fingerprint = query_fingerprint((directory, search, pinned, archived, order))
        start = self._cursor_start(records, cursor, fingerprint)
        page = records[start : start + limit]
        next_cursor = None
        if start + limit < len(records) and page:
            next_cursor = encode_cursor(query=fingerprint, anchor_id=page[-1].id)
        return SessionListResponse(
            data=[self.session_info_from_index(record) for record in page],
            cursor=CursorInfo(previous=None, next=next_cursor),
        )

    def get_session(self, session_id: str) -> SessionInfo:
        if not self.store.exists(session_id):
            raise ApiError(404, "session_not_found", "Session was not found.")
        entry = self.store.index_entry(session_id)
        if entry is not None:
            return self.session_info_from_index(entry)
        return self.session_info(self._require_record(session_id))

    def create_session(self, request: SessionCreateRequest) -> SessionInfo:
        root = str(Path(request.location.directory).resolve())
        session_id = request.id or new_unique_session_id(self.store.exists)
        if self.store.exists(session_id):
            existing = self.store.summary_record(session_id)
            if existing is None:
                raise ApiError(404, "session_not_found", "Session was not found.")
            if existing.root is not None and str(Path(existing.root).resolve()) != root:
                raise ApiError(
                    409,
                    "session_identity_conflict",
                    "Session id already exists at another location.",
                    {"sessionID": session_id, "location": existing.root},
                )
            entry = self.store.index_entry(session_id)
            return (
                self.session_info_from_index(entry)
                if entry is not None
                else self.session_info(existing)
            )
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
        archived_set: bool,
        archived: bool | None,
    ) -> SessionInfo:
        if not title_set and not pinned_set and not archived_set:
            raise ApiError(400, "empty_mutation", "At least one field is required.")
        record = self.store.summary_record(session_id)
        if record is None:
            raise ApiError(404, "session_not_found", "Session was not found.")
        if title_set:
            record = self.store.set_title(session_id, title, existing_record=record)
        if pinned_set:
            if pinned is None:
                raise ApiError(400, "invalid_pinned", "pinned cannot be null.")
            record = self.store.set_pinned(session_id, pinned, existing_record=record)
        if archived_set:
            if archived is None:
                raise ApiError(400, "invalid_archived", "archived cannot be null.")
            record = self.store.set_archived(
                session_id, archived, existing_record=record
            )
        self._publish(
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
            self.session_info_from_index(entry)
            if entry is not None
            else self.session_info(record)
        )

    def fork_session(self, session_id: str, request: SessionForkRequest) -> SessionInfo:
        source_summary = self.store.summary_record(session_id)
        if source_summary is None:
            raise ApiError(404, "session_not_found", "Session was not found.")
        fork_id = request.id or new_unique_session_id(self.store.exists)
        if self.store.exists(fork_id):
            raise ApiError(409, "session_identity_conflict", "Fork id already exists.")
        if request.from_entry_id is None:
            forked = self.store.fork(
                session_id,
                new_session_id_value=fork_id,
                title=request.title,
                materialize_result=False,
            )
            self.message_index.clone_sidecar(session_id, fork_id)
            self._publish(
                forked,
                "session.created",
                {"sessionID": forked.id, "sourceSessionID": session_id},
            )
            fork_entry = self.store.index_entry(fork_id)
            return (
                self.session_info_from_index(fork_entry)
                if fork_entry is not None
                else self.session_info(forked)
            )

        indexed_target = self.message_index.navigation_target(
            session_id,
            request.from_entry_id,
        )
        if indexed_target is not None:
            forked = self.store.fork(
                session_id,
                new_session_id_value=fork_id,
                title=request.title,
                selected_leaf_id=indexed_target.id,
                materialize_result=False,
            )
            self.message_index.clone_sidecar(session_id, fork_id)
            self._publish(
                forked,
                "session.created",
                {
                    "sessionID": forked.id,
                    "sourceSessionID": session_id,
                    "fromEntryID": request.from_entry_id,
                },
            )
            fork_entry = self.store.index_entry(fork_id)
            return (
                self.session_info_from_index(fork_entry)
                if fork_entry is not None
                else self.session_info(forked)
            )

        source = self._require_record(session_id)
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
        snapshot_seq = (
            self.events.journal.latest_sequence(session_id)
            if self.events is not None
            else 0
        )
        fingerprint = query_fingerprint((session_id, "active", order))
        anchor = (
            decode_cursor(cursor, expected_query=fingerprint)
            if cursor is not None
            else None
        )
        indexed_page = self.message_index.page(
            session_id,
            limit=limit,
            order=order,
            anchor_id=anchor,
        )
        if indexed_page is not None:
            page = [project_entry(entry) for entry in indexed_page.entries]
            next_cursor = (
                encode_cursor(query=fingerprint, anchor_id=page[-1].id)
                if indexed_page.has_more and page
                else None
            )
            return MessageListResponse(
                data=page,
                cursor=CursorInfo(previous=None, next=next_cursor),
                snapshot_seq=snapshot_seq,
            )

        snapshot = self._require_snapshot(session_id)
        messages: Sequence[object] = (
            tuple(reversed(snapshot.active_entries))
            if order == "desc"
            else snapshot.active_entries
        )
        start = self._message_cursor_start(messages, cursor, fingerprint)
        page_entries = messages[start : start + limit]
        page = [project_entry(entry) for entry in page_entries]
        next_cursor = (
            encode_cursor(query=fingerprint, anchor_id=page[-1].id)
            if start + limit < len(messages) and page
            else None
        )
        return MessageListResponse(
            data=page,
            cursor=CursorInfo(previous=None, next=next_cursor),
            snapshot_seq=snapshot_seq,
        )

    def message(self, session_id: str, message_id: str) -> MessageResponse:
        indexed = self.message_index.entry(session_id, message_id)
        if indexed is not None:
            return MessageResponse(data=project_entry(indexed))
        snapshot = self._require_snapshot(session_id)
        entry = snapshot.entries_by_id.get(message_id)
        if entry is not None and entry.kind not in {"instruction", "memory_snapshot"}:
            return MessageResponse(data=project_entry(entry))
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
        """Return a bounded recent model-visible conversation projection."""
        indexed = self.message_index.context_window(
            session_id,
            limit=limit,
            include_instructions=include_system,
        )
        if indexed is not None:
            source_messages: list[Message] = []
            for entry in indexed.entries:
                if entry.kind == "instruction":
                    if include_system and entry.message is not None:
                        source_messages.append(entry.message)
                    continue
                if entry.kind == "memory_snapshot":
                    try:
                        snapshot = MemorySnapshot.model_validate(entry.metadata)
                    except ValueError:
                        continue
                    handoff = snapshot.compaction_handoff
                    if handoff is not None:
                        source_messages.extend(handoff.retained_messages)
                    source_messages.append(
                        entry.message or Message.assistant(snapshot.summary_text)
                    )
                    continue
                if entry.kind == "compaction_summary":
                    continue
                if entry.message is not None:
                    source_messages.append(entry.message)
            projected = self._context_messages(
                source_messages,
                include_system=include_system,
                include_tool_results=include_tool_results,
            )
            bounded, retained_chars, chars_truncated = self._bound_context_messages(
                projected,
                max_chars=max_chars,
            )
            return ContextResponse(
                data=ContextData(
                    messages=bounded,
                    total_entries=indexed.total_entries,
                    retained_entries=len(indexed.entries),
                    retained_chars=retained_chars,
                    max_chars=max_chars,
                    truncated=indexed.truncated or chars_truncated,
                )
            )

        # Legacy/noncanonical persistence falls back to the exact projection,
        # but the HTTP response is still bounded.
        record = self._require_snapshot(session_id).record
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
        total_entries = len(view.active_entries)
        if limit and len(source_messages) > limit:
            source_messages = source_messages[-limit:]
        projected = self._context_messages(
            source_messages,
            include_system=include_system,
            include_tool_results=include_tool_results,
        )
        bounded, retained_chars, chars_truncated = self._bound_context_messages(
            projected,
            max_chars=max_chars,
        )
        return ContextResponse(
            data=ContextData(
                messages=bounded,
                total_entries=total_entries,
                retained_entries=min(total_entries, limit),
                retained_chars=retained_chars,
                max_chars=max_chars,
                truncated=total_entries > limit or chars_truncated,
            )
        )

    @staticmethod
    def _context_messages(
        source_messages: Sequence[Message],
        *,
        include_system: bool,
        include_tool_results: bool,
    ) -> list[ContextMessage]:
        messages: list[ContextMessage] = []
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
        return messages

    @staticmethod
    def _bound_context_messages(
        messages: Sequence[ContextMessage],
        *,
        max_chars: int,
    ) -> tuple[list[ContextMessage], int, bool]:
        retained: list[ContextMessage] = []
        remaining = max_chars
        truncated = False
        for message in reversed(messages):
            text_chars = sum(
                len(part.text) for part in message.content if part.type == "text"
            )
            if text_chars <= remaining:
                retained.append(message)
                remaining -= text_chars
                continue
            if remaining <= 0:
                truncated = True
                break
            content = []
            for part in message.content:
                if part.type != "text":
                    content.append(part)
                    continue
                if remaining <= 0:
                    continue
                if len(part.text) <= remaining:
                    content.append(part)
                    remaining -= len(part.text)
                    continue
                content.append(part.model_copy(update={"text": part.text[:remaining]}))
                remaining = 0
                truncated = True
            retained.append(message.model_copy(update={"content": content}))
            break
        if len(retained) < len(messages):
            truncated = True
        retained.reverse()
        return retained, max_chars - remaining, truncated

    def tree(
        self,
        session_id: str,
        *,
        limit: int,
        cursor: str | None,
    ) -> TreeResponse:
        fingerprint = query_fingerprint((session_id, "tree"))
        anchor = (
            decode_cursor(cursor, expected_query=fingerprint)
            if cursor is not None
            else None
        )
        indexed = self.message_index.tree_page(
            session_id,
            limit=limit,
            anchor_id=anchor,
        )
        if indexed is not None:
            next_cursor = (
                encode_cursor(
                    query=fingerprint,
                    anchor_id=indexed.entries[0].id,
                )
                if indexed.has_more and indexed.entries
                else None
            )
            summary = self.store.summary_record(session_id)
            return TreeResponse(
                data=TreeData(
                    revision=_revision_from_timestamp(
                        summary.updated_at if summary is not None else None
                    ),
                    leaf_id=indexed.leaf_id,
                    entries=[
                        project_tree_entry(
                            entry,
                            leaf_id=indexed.leaf_id,
                            active=entry.id in indexed.active_ids,
                            child_count=indexed.child_counts.get(entry.id, 0),
                        )
                        for entry in indexed.entries
                    ],
                    total_entries=indexed.total_entries,
                    cursor=CursorInfo(previous=None, next=next_cursor),
                )
            )

        record = self._require_snapshot(session_id).record
        all_entries = project_tree(record.conversation_entries, record.leaf_id)
        end = len(all_entries)
        if anchor is not None:
            try:
                end = next(
                    index
                    for index, entry in enumerate(all_entries)
                    if entry.id == anchor
                )
            except StopIteration as exc:
                raise ApiError(
                    400, "invalid_cursor_anchor", "Cursor anchor no longer exists."
                ) from exc
        start = max(0, end - limit)
        page = all_entries[start:end]
        next_cursor = (
            encode_cursor(query=fingerprint, anchor_id=page[0].id)
            if start > 0 and page
            else None
        )
        return TreeResponse(
            data=TreeData(
                revision=_revision_from_timestamp(record.updated_at),
                leaf_id=record.leaf_id,
                entries=page,
                total_entries=len(all_entries),
                cursor=CursorInfo(previous=None, next=next_cursor),
            )
        )

    def navigation_preview(
        self,
        session_id: str,
        *,
        target_id: str,
        include_abandoned: bool,
    ) -> TreeNavigationPreviewResponse:
        indexed = self.message_index.navigation_preview(
            session_id,
            target_id=target_id,
            abandoned_limit=100 if include_abandoned else 0,
        )
        if indexed is not None:
            editor_text = (
                indexed.target.message.display_text_content() or ""
                if indexed.target.kind == "user" and indexed.target.message is not None
                else None
            )
            abandoned = [
                TreeNavigationAbandonedEntry(
                    id=entry.id,
                    kind=entry.kind,
                    preview=_entry_preview_text(entry),
                )
                for entry in indexed.abandoned
            ]
            return TreeNavigationPreviewResponse(
                data=TreeNavigationPreviewData(
                    target_id=target_id,
                    current=indexed.current,
                    editor_text=editor_text,
                    abandoned=abandoned,
                    abandoned_total=(
                        indexed.abandoned_total if include_abandoned else 0
                    ),
                    abandoned_truncated=(
                        indexed.abandoned_truncated if include_abandoned else False
                    ),
                )
            )

        record = self._require_snapshot(session_id).record
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
            text = (
                message.display_text_content()
                if message is not None
                else item.summary_text
            )
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
                abandoned_total=len(abandoned),
                abandoned_truncated=False,
            )
        )

    def navigate_tree(
        self,
        session_id: str,
        request: TreeNavigateRequest,
    ) -> TreeNavigateResponse:
        summary = self.store.summary_record(session_id)
        if summary is None:
            raise ApiError(404, "session_not_found", "Session was not found.")
        self._require_tree_revision(summary, request.expected_revision)
        indexed_target = self.message_index.navigation_target(
            session_id,
            request.target_id,
        )
        if indexed_target is not None:
            if indexed_target.id == summary.leaf_id:
                return TreeNavigateResponse(
                    data=TreeNavigateData(
                        revision=_revision_from_timestamp(summary.updated_at),
                        leaf_id=summary.leaf_id,
                        editor_text=None,
                        summary_added=False,
                    )
                )
            editor_text = (
                indexed_target.message.display_text_content() or ""
                if indexed_target.kind == "user" and indexed_target.message is not None
                else None
            )
            selected_leaf = (
                indexed_target.parent_id
                if editor_text is not None
                else indexed_target.id
            )
            if selected_leaf is None:
                raise ApiError(
                    409,
                    "tree_root_navigation_unsupported",
                    "This legacy tree has no persisted parent before the selected user entry.",
                )
            normalized_summary = (request.branch_summary or "").strip()
            appended: tuple[ConversationEntry, ...] = ()
            final_leaf = selected_leaf
            if normalized_summary:
                branch_entry = ConversationEntry(
                    kind="branch_summary",
                    message=Message.user(
                        "Branch summary from the path you left:\n\n"
                        f"{normalized_summary}"
                    ),
                    metadata={
                        "from_leaf_id": summary.leaf_id,
                        "target_id": request.target_id,
                        "summary": normalized_summary,
                    },
                    parent_id=selected_leaf,
                )
                appended = (branch_entry,)
                final_leaf = branch_entry.id
            updated = self.store.save_indexed_tree_navigation(
                session_id,
                existing_record=summary,
                leaf_id=final_leaf,
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
                    editor_text=editor_text,
                    summary_added=bool(appended),
                )
            )

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
        fallback_appended = list(exported.entries[len(record.conversation_entries) :])
        index = SessionTreeIndex(record.conversation_entries, record.leaf_id)
        updated = self.store.save_tree_delta(
            session_id,
            existing_record=record,
            tree_index=index,
            leaf_id=exported.leaf_id,
            appended_entries=fallback_appended,
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
        summary = self.store.summary_record(session_id)
        if summary is None:
            raise ApiError(404, "session_not_found", "Session was not found.")
        self._require_tree_revision(summary, expected_revision)
        indexed_state = self.message_index.entry_tree_state(session_id, entry_id)
        if indexed_state is not None:
            entry, active, current, child_count = indexed_state
            normalized = " ".join((label or "").split()).strip()
            metadata = dict(entry.metadata)
            if normalized:
                metadata["label"] = normalized
            else:
                metadata.pop("label", None)
            updated_entry = entry.model_copy(
                update={"metadata": metadata},
                deep=True,
            )
            updated = self.store.save_indexed_entry_metadata(
                session_id,
                updated_entry,
                existing_record=summary,
            )
            revision = _revision_from_timestamp(updated.updated_at)
            self._publish(
                updated,
                "session.tree.updated",
                {"revision": revision, "entryID": entry_id},
            )
            return TreeEntryPatchResponse(
                data=TreeEntryPatchData(
                    revision=revision,
                    entry=project_tree_entry(
                        updated_entry,
                        leaf_id=entry_id if current else summary.leaf_id,
                        active=active,
                        child_count=child_count,
                    ),
                )
            )

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
        return SessionInfo(
            id=record.id,
            title=record.title,
            pinned=record.pinned,
            archived_at=record.archived_at,
            location=LocationInfo(directory=record.root or ""),
            time=SessionTime(created=record.created_at, updated=record.updated_at),
            selection=SessionSelection(
                provider=record.provider_name,
                model=record.model_id,
                reasoning_effort=record.reasoning_effort,
            ),
            context_usage=(
                dict(record.context_usage) if record.context_usage is not None else None
            ),
            tree=SessionTreeSummary(
                leaf_id=record.leaf_id,
                entry_count=len(record.conversation_entries),
            ),
            queue=self._queue_summary(record.id),
        )

    def session_info_from_index(self, entry: SessionIndexEntry) -> SessionInfo:
        """Build a list-card projection without loading conversation history."""
        return SessionInfo(
            id=entry.id,
            title=entry.title,
            pinned=entry.pinned,
            archived_at=entry.archived_at,
            location=LocationInfo(directory=entry.root or ""),
            time=SessionTime(created=entry.created_at, updated=entry.updated_at),
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
                entry_count=entry.entry_count or 0,
            ),
            queue=self._queue_summary(entry.id),
        )

    def _queue_summary(self, session_id: str) -> SessionQueueSummary:
        queue = load_prompt_queue_snapshot(self.store.directory, session_id)
        steering = sum(item.kind == "steering" for item in queue.prompts)
        paused = sum(item.paused for item in queue.prompts)
        return SessionQueueSummary(
            total=len(queue.prompts),
            steering=steering,
            queued=len(queue.prompts) - steering,
            paused=paused,
            revision=queue.revision,
        )

    def _require_record(self, session_id: str) -> SessionRecord:
        if not self.store.exists(session_id):
            raise ApiError(404, "session_not_found", "Session was not found.")
        return self.store.load(session_id)

    def _require_snapshot(self, session_id: str) -> SessionReadSnapshot:
        if not self.store.exists(session_id):
            raise ApiError(404, "session_not_found", "Session was not found.")
        try:
            return self.read_cache.get(session_id)
        except FileNotFoundError as exc:
            raise ApiError(404, "session_not_found", "Session was not found.") from exc

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
    def _sort_key(
        record: SessionRecord | SessionIndexEntry,
        order: SessionOrder,
    ) -> tuple[str, str]:
        value = (
            record.created_at
            if order.startswith("created")
            else record.updated_at or record.created_at
        )
        return value or "", record.id

    @staticmethod
    def _cursor_start(
        records: Sequence[SessionRecord | SessionIndexEntry],
        cursor: str | None,
        fingerprint: str,
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


def _entry_preview_text(entry: ConversationEntry) -> str | None:
    if entry.message is not None:
        return _preview_text(entry.message.display_text_content())
    summary = entry.metadata.get("summary")
    return _preview_text(summary if isinstance(summary, str) else None)
