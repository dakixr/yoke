"""Session tree navigation helpers."""

from __future__ import annotations

from dataclasses import dataclass

from yoke.agent.loop.agent import RuntimeAgent
from yoke.agent.models import Message
from yoke.agent.session_tree import BranchEntryView
from yoke.agent.session_tree import ConversationProjection
from yoke.agent.session_tree import ConversationView
from yoke.agent.session_tree import SessionTree
from yoke.ai.providers.base import complete_with_cancel
from yoke.ai.providers.usage_context import usage_metric_context
from yoke.cli.runtime.base import ActiveSession
from yoke.cli.runtime.session import save_active_session
from yoke.cli.runtime.tree_view import TreeFilterMode as TreeFilterMode
from yoke.cli.runtime.tree_view import TreeNode as TreeNode
from yoke.cli.runtime.tree_view import TreeRow as TreeRow
from yoke.cli.runtime.tree_view import (
    default_folded_tree_ids as default_folded_tree_ids,
)
from yoke.cli.runtime.tree_view import (
    flatten_tree_rows as flatten_tree_rows,
)
from yoke.cli.runtime.tree_view import (
    get_session_tree as get_session_tree,
)
from yoke.cli.runtime.tree_view import (
    set_entry_label as set_entry_label,
)


@dataclass(slots=True)
class TreeNavigationResult:
    """Result of moving the active session leaf."""

    messages: list[Message]
    active_session: ActiveSession
    editor_text: str | None = None
    summary_created: bool = False


def navigate_session_tree(
    active_session: ActiveSession,
    agent: object,
    target_id: str,
    *,
    summarize: bool = False,
    custom_instructions: str | None = None,
) -> TreeNavigationResult:
    """Move the session leaf to a tree entry and rebuild active messages."""
    entry_count = len(active_session.record.conversation_entries)
    tree = SessionTree.borrow_validated(
        active_session.record.conversation_entries,
        active_session.record.leaf_id,
    )
    target = tree.ref_from_persisted_id(target_id)
    preview = tree.preview_navigation(
        target,
        include_abandoned=summarize,
    )
    if preview.current:
        return TreeNavigationResult(
            messages=_transcript(tree),
            active_session=active_session,
        )

    summary: str | None = None
    if summarize and preview.abandoned:
        summary = summarize_branch_entries(
            agent,
            preview.abandoned,
            custom_instructions=custom_instructions,
        )
    outcome = tree.navigate(target, branch_summary=summary)
    projection = tree.project(ConversationProjection())
    messages = _transcript_projection(projection)
    delta = tree.export_append_delta(entry_count)
    if delta.leaf_id is None:
        exported = tree.export_for_persistence()
        save_active_session(
            active_session,
            messages,
            conversation_entries=list(exported.entries),
            leaf_id=exported.leaf_id,
            agent=agent,
        )
    else:
        with active_session.save_lock:
            active_session.record = active_session.store.save_tree_delta(
                active_session.id,
                existing_record=active_session.record,
                tree_index=active_session.tree_index,
                leaf_id=delta.leaf_id,
                appended_entries=delta.entries,
            )
    _load_agent_branch(agent, projection, active_session)
    return TreeNavigationResult(
        messages=messages,
        active_session=active_session,
        editor_text=outcome.editor_text,
        summary_created=outcome.summary_appended,
    )


def summarize_branch_entries(
    agent: object,
    entries: tuple[BranchEntryView, ...],
    *,
    custom_instructions: str | None = None,
) -> str | None:
    """Generate a concise summary for abandoned branch entries."""
    if not isinstance(agent, RuntimeAgent):
        return None
    rendered = "\n\n".join(_entry_summary_text(entry) for entry in entries)
    guidance = (custom_instructions or "").strip()
    prompt = (
        "Summarize the following abandoned conversation branch so it can be "
        "used as compact context on a new branch. Preserve concrete decisions, "
        "files touched, errors, commands, and unresolved next steps."
    )
    if guidance:
        prompt += f"\n\nAdditional user guidance:\n{guidance}"
    with usage_metric_context(call_kind="branch_summary"):
        response = complete_with_cancel(
            agent.provider,
            [Message.system(prompt), Message.user(rendered)],
            [],
        )
    summary = (response.plain_text_content or "").strip()
    return summary or None


def _load_agent_branch(
    agent: object,
    projection: ConversationView,
    active_session: ActiveSession,
) -> None:
    if not isinstance(agent, RuntimeAgent):
        return
    agent.load_conversation(
        conversation_entries=list(projection.runtime_entries),
        available_skills=agent.available_skills,
        active_skills=active_session.record.active_skills,
    )


def _transcript(tree: SessionTree) -> list[Message]:
    projection = tree.project(ConversationProjection())
    return _transcript_projection(projection)


def _transcript_projection(projection: ConversationView) -> list[Message]:
    return [message.model_copy(deep=True) for message in projection.transcript_messages]


def _entry_summary_text(entry: BranchEntryView) -> str:
    if entry.kind == "tool_result":
        return ""
    if entry.message is None:
        if entry.summary_text is not None:
            return f"[{entry.kind}] {entry.summary_text}"
        return f"[{entry.kind}]"
    text = entry.message.text_content() or ""
    return f"[{entry.kind}/{entry.message.role}] {text}"
