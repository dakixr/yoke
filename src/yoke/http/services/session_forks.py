"""Copy saved branches with an explicit executable destination workspace."""

from __future__ import annotations

from collections.abc import Callable

from yoke.agent.session_tree import SessionTree
from yoke.agent.session_tree.projections import ConversationProjection
from yoke.http.errors import ApiError, session_not_found
from yoke.http.models.session import SessionForkRequest
from yoke.http.reads.message_index import SessionMessageIndex
from yoke.http.reads.model import SessionReadModel
from yoke.session import (
    SessionRecord,
    SessionStore,
    fork_session_title,
    new_unique_session_id,
)
from yoke.session.workspace import require_session_workspace, require_workspace


class SessionForks:
    """Own branch copies with explicit storage and event dependencies."""

    def __init__(
        self,
        store: SessionStore,
        message_index: SessionMessageIndex,
        read_model: SessionReadModel,
        publish: Callable[[SessionRecord, str, dict[str, object]], None],
    ) -> None:
        self.store = store
        self.message_index = message_index
        self.read_model = read_model
        self.publish = publish

    def fork(self, session_id: str, request: SessionForkRequest) -> SessionRecord:
        """Validate the target before copying anything, retaining the source history."""
        source_summary = self.store.summary_record(session_id)
        if source_summary is None:
            raise session_not_found()
        target = (
            require_workspace(request.location.directory)
            if request.location is not None
            else require_session_workspace(source_summary)
        )
        fork_id = request.id or new_unique_session_id(self.store.exists)
        if self.store.exists(fork_id):
            raise ApiError(409, "session_identity_conflict", "Fork id already exists.")
        indexed_target = (
            self.message_index.navigation_target(session_id, request.from_entry_id)
            if request.from_entry_id is not None
            else None
        )
        if request.from_entry_id is None or indexed_target is not None:
            forked = self.store.fork(
                session_id,
                new_session_id_value=fork_id,
                root=target,
                title=request.title,
                selected_leaf_id=indexed_target.id
                if indexed_target is not None
                else None,
                materialize_result=False,
            )
            data: dict[str, object] = {
                "sessionID": forked.id,
                "sourceSessionID": session_id,
            }
            if request.from_entry_id is not None:
                data["fromEntryID"] = request.from_entry_id
            self.publish(forked, "session.created", data)
            return forked

        source = self.read_model.record(session_id)
        tree = SessionTree.restore(source.conversation_entries, source.leaf_id)
        try:
            ref = tree.ref_from_persisted_id(request.from_entry_id)
        except ValueError as exc:
            raise ApiError(404, "entry_not_found", "Tree entry was not found.") from exc
        tree.checkout(ref)
        exported = tree.export_for_persistence()
        transcript = tree.project(ConversationProjection()).transcript_messages
        forked = self.store.save(
            fork_id,
            list(transcript),
            conversation_entries=list(exported.entries),
            leaf_id=exported.leaf_id,
            active_skills=source.active_skills,
            skill_dirs=source.skill_dirs if source.root == str(target) else [],
            root=target,
            title=request.title or fork_session_title(source.title),
            provider_name=source.provider_name,
            model_id=source.model_id,
            reasoning_effort=source.reasoning_effort,
            context_window_tokens=source.context_window_tokens,
        )
        self.publish(
            forked,
            "session.created",
            {
                "sessionID": forked.id,
                "sourceSessionID": session_id,
                "fromEntryID": request.from_entry_id,
            },
        )
        return forked
