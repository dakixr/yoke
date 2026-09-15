"""Copy saved branches with an explicit executable destination workspace."""

from __future__ import annotations

from typing import TYPE_CHECKING

from yoke.agent.session_tree import SessionTree
from yoke.agent.session_tree.projections import ConversationProjection
from yoke.http.errors import ApiError
from yoke.http.models.session import SessionForkRequest, SessionInfo
from yoke.session import fork_session_title, new_unique_session_id
from yoke.session.workspace import require_session_workspace, require_workspace

if TYPE_CHECKING:
    from yoke.http.services.session_service import SessionService


def fork_session(
    service: SessionService, session_id: str, request: SessionForkRequest
) -> SessionInfo:
    """Validate the target before copying anything, retaining the source history."""
    source_summary = service.store.summary_record(session_id)
    if source_summary is None:
        raise ApiError(404, "session_not_found", "Session was not found.")
    target = (
        require_workspace(request.location.directory)
        if request.location is not None
        else require_session_workspace(source_summary)
    )
    fork_id = request.id or new_unique_session_id(service.store.exists)
    if service.store.exists(fork_id):
        raise ApiError(409, "session_identity_conflict", "Fork id already exists.")
    indexed_target = (
        service.message_index.navigation_target(session_id, request.from_entry_id)
        if request.from_entry_id is not None
        else None
    )
    if request.from_entry_id is None or indexed_target is not None:
        forked = service.store.fork(
            session_id,
            new_session_id_value=fork_id,
            root=target,
            title=request.title,
            selected_leaf_id=indexed_target.id if indexed_target is not None else None,
            materialize_result=False,
        )
        service.message_index.clone_sidecar(session_id, fork_id)
        data: dict[str, object] = {
            "sessionID": forked.id,
            "sourceSessionID": session_id,
        }
        if request.from_entry_id is not None:
            data["fromEntryID"] = request.from_entry_id
        service._publish(forked, "session.created", data)
        fork_entry = service.store.index_entry(fork_id)
        return (
            service.session_info_from_index(fork_entry)
            if fork_entry is not None
            else service.session_info(forked)
        )

    source = service._require_record(session_id)
    tree = SessionTree.restore(source.conversation_entries, source.leaf_id)
    try:
        ref = tree.ref_from_persisted_id(request.from_entry_id)
    except ValueError as exc:
        raise ApiError(404, "entry_not_found", "Tree entry was not found.") from exc
    tree.checkout(ref)
    exported = tree.export_for_persistence()
    transcript = tree.project(ConversationProjection()).transcript_messages
    forked = service.store.save(
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
    service._publish(
        forked,
        "session.created",
        {
            "sessionID": forked.id,
            "sourceSessionID": session_id,
            "fromEntryID": request.from_entry_id,
        },
    )
    return service.session_info(forked)
