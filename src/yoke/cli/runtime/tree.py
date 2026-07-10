"""Session tree navigation helpers."""

from __future__ import annotations

from dataclasses import dataclass

from yoke.agent.loop import RuntimeAgent
from yoke.agent.loop import ConversationEntryHistory
from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.agent.state import active_branch_entries
from yoke.agent.state import migrate_conversation_tree
from yoke.agent.state import transcript_messages_from_entries
from yoke.cli.runtime.base import ActiveSession
from yoke.cli.runtime.session import save_active_session
from yoke.cli.runtime.tree_view import TreeFilterMode as TreeFilterMode
from yoke.cli.runtime.tree_view import TreeNode as TreeNode
from yoke.cli.runtime.tree_view import TreeRow as TreeRow
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
    summary_error: str | None = None


def navigate_session_tree(
    active_session: ActiveSession,
    agent: object,
    target_id: str,
    *,
    summarize: bool = False,
    custom_instructions: str | None = None,
) -> TreeNavigationResult:
    """Move the session leaf to a tree entry and rebuild active messages."""
    entries, old_leaf_id, _changed = migrate_conversation_tree(
        active_session.record.conversation_entries,
        leaf_id=active_session.record.leaf_id,
    )
    by_id = {entry.id: entry for entry in entries}
    target = by_id.get(target_id)
    if target is None:
        raise ValueError(f"Tree entry not found: {target_id}")
    if target_id == old_leaf_id:
        return TreeNavigationResult(
            messages=transcript_messages_from_entries(
                entries,
                leaf_id=old_leaf_id,
            ),
            active_session=active_session,
        )

    new_leaf_id = target_id
    editor_text = None
    if target.kind == "user" and target.message is not None:
        new_leaf_id = target.parent_id
        editor_text = target.message.display_text_content() or ""

    summary_created = False
    summary_error = None
    if summarize:
        abandoned = collect_abandoned_branch_entries(
            entries,
            old_leaf_id=old_leaf_id,
            target_id=target_id,
        )
        if abandoned:
            try:
                summary = summarize_branch_entries(
                    agent,
                    abandoned,
                    custom_instructions=custom_instructions,
                )
            except Exception as exc:  # noqa: BLE001
                summary = None
                summary_error = str(exc).strip() or type(exc).__name__
            if summary:
                summary_entry = ConversationEntry(
                    kind="branch_summary",
                    parent_id=new_leaf_id,
                    message=Message.user(
                        f"Branch summary from the path you left:\n\n{summary}"
                    ),
                    metadata={
                        "from_leaf_id": old_leaf_id,
                        "target_id": target_id,
                        "summary": summary,
                    },
                )
                entries.append(summary_entry)
                new_leaf_id = summary_entry.id
                summary_created = True

    save_active_session(
        active_session,
        transcript_messages_from_entries(entries, leaf_id=new_leaf_id),
        conversation_entries=entries,
        leaf_id=new_leaf_id,
        agent=agent,
    )
    messages = active_session.record.messages
    _load_agent_branch(agent, active_session)
    return TreeNavigationResult(
        messages=messages,
        active_session=active_session,
        editor_text=editor_text,
        summary_created=summary_created,
        summary_error=summary_error,
    )


def collect_abandoned_branch_entries(
    entries: list[ConversationEntry],
    *,
    old_leaf_id: str | None,
    target_id: str,
) -> list[ConversationEntry]:
    """Collect entries on the old branch that are not on the target path."""
    if old_leaf_id is None:
        return []
    by_id = {entry.id: entry for entry in entries}
    target_path = _path_to_root(by_id, target_id)
    old_path = _path_to_root(by_id, old_leaf_id)
    target_ids = {entry.id for entry in target_path}
    common_id = next(
        (entry.id for entry in reversed(old_path) if entry.id in target_ids),
        None,
    )
    abandoned: list[ConversationEntry] = []
    current_id = old_leaf_id
    while current_id is not None and current_id != common_id:
        entry = by_id.get(current_id)
        if entry is None:
            break
        abandoned.append(entry.model_copy(deep=True))
        current_id = entry.parent_id
    abandoned.reverse()
    return abandoned


def summarize_branch_entries(
    agent: object,
    entries: list[ConversationEntry],
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
    response = agent.provider.complete(
        [Message.system(prompt), Message.user(rendered)],
        [],
    )
    summary = (response.plain_text_content or "").strip()
    return summary or None


def _load_agent_branch(agent: object, active_session: ActiveSession) -> None:
    if not isinstance(agent, RuntimeAgent):
        return
    agent.load_conversation(
        ConversationEntryHistory(
            active_branch_entries(
                active_session.record.conversation_entries,
                leaf_id=active_session.record.leaf_id,
            )
            or []
        ),
        available_skills=agent.available_skills,
        active_skills=active_session.record.active_skills,
    )


def _path_to_root(
    by_id: dict[str, ConversationEntry],
    leaf_id: str | None,
) -> list[ConversationEntry]:
    path: list[ConversationEntry] = []
    current_id = leaf_id
    seen: set[str] = set()
    while current_id is not None and current_id not in seen:
        seen.add(current_id)
        entry = by_id.get(current_id)
        if entry is None:
            break
        path.append(entry)
        current_id = entry.parent_id
    path.reverse()
    return path


def _entry_summary_text(entry: ConversationEntry) -> str:
    if entry.kind == "tool_result":
        return ""
    if entry.message is None:
        summary = entry.metadata.get("summary")
        if isinstance(summary, str):
            return f"[{entry.kind}] {summary}"
        return f"[{entry.kind}]"
    text = entry.message.display_text_content() or ""
    return f"[{entry.kind}/{entry.message.role}] {text}"
