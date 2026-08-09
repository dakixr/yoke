"""Persistence adapter for the authoritative session-tree module."""

from __future__ import annotations

import builtins
from dataclasses import dataclass
from typing import TYPE_CHECKING

from yoke.agent.message_sanitizer import normalize_tool_call_sequence
from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.agent.session_tree import SessionTree
from yoke.cli.session.tree_index import SessionTreeIndex

if TYPE_CHECKING:
    from yoke.cli.session import SessionRecord


@dataclass(frozen=True, slots=True)
class ResolvedConversationTree:
    """Resolved topology and optional proof of an append-only update."""

    entries: builtins.list[ConversationEntry]
    leaf_id: str | None
    appended_entries: tuple[ConversationEntry, ...] | None


def _resolve_saved_conversation_tree(
    existing: SessionRecord,
    messages: builtins.list[Message],
    *,
    conversation_entries: builtins.list[ConversationEntry] | None,
    leaf_id: str | None,
    tree_index: SessionTreeIndex | None = None,
) -> ResolvedConversationTree:
    """Reconcile one runtime branch through the session-tree seam."""
    if conversation_entries is not None:
        if tree_index is not None:
            appended = _resolve_active_append(
                existing,
                conversation_entries,
                leaf_id=leaf_id,
                tree_index=tree_index,
            )
            if appended is not None:
                return appended
        tree = SessionTree.restore(
            existing.conversation_entries,
            existing.leaf_id,
        )
        incoming = _sanitize_conversation_entries(conversation_entries)
    else:
        tree = SessionTree.import_legacy(
            existing.conversation_entries,
            existing.leaf_id,
        )
        incoming = _sanitize_conversation_entries(
            list(SessionTree.from_messages(messages).entries)
        )
    if incoming:
        if tree.entries:
            tree.reconcile(incoming, leaf_id=leaf_id)
        else:
            tree = SessionTree.import_legacy(incoming, leaf_id)
    elif leaf_id is not None and tree.entries:
        tree = SessionTree.import_legacy(tree.entries, leaf_id)
    exported = tree.export()
    return ResolvedConversationTree(
        entries=list(exported.entries),
        leaf_id=exported.leaf_id,
        appended_entries=None,
    )


def _resolve_active_append(
    existing: SessionRecord,
    incoming: builtins.list[ConversationEntry],
    *,
    leaf_id: str | None,
    tree_index: SessionTreeIndex,
) -> ResolvedConversationTree | None:
    active = tree_index.active_entry_refs()
    if len(incoming) < len(active) or any(
        entry is not incoming[index] and entry != incoming[index]
        for index, entry in enumerate(active)
    ):
        return None
    suffix = _sanitize_append_suffix(
        incoming[len(active) :],
        parent_id=tree_index.leaf_id,
    )
    selected_leaf = leaf_id or (suffix[-1].id if suffix else tree_index.leaf_id)
    proven = tree_index.prove_active_append(
        [*active, *suffix],
        selected_leaf,
    )
    if proven is None:
        return None
    return ResolvedConversationTree(
        entries=existing.conversation_entries,
        leaf_id=selected_leaf,
        appended_entries=proven,
    )


def _sanitize_append_suffix(
    entries: builtins.list[ConversationEntry],
    *,
    parent_id: str | None,
) -> builtins.list[ConversationEntry]:
    normalized_entries = [_normalize_conversation_entry(entry) for entry in entries]
    normalized_messages = normalize_tool_call_sequence(
        (entry.message for entry in normalized_entries if entry.message is not None),
        drop_incomplete_assistant=True,
    )
    normalized_iter = iter(normalized_messages)
    next_message = next(normalized_iter, None)
    parent_mapping: dict[str, str | None] = {}
    sanitized: builtins.list[ConversationEntry] = []
    for entry in normalized_entries:
        source_parent = entry.parent_id
        resolved_parent = (
            parent_mapping[source_parent]
            if source_parent is not None and source_parent in parent_mapping
            else source_parent
        )
        keep = entry.message is None or (
            next_message is not None and entry.message == next_message
        )
        if not keep:
            parent_mapping[entry.id] = resolved_parent
            continue
        message = None
        if entry.message is not None:
            if next_message is None:
                continue
            message = next_message.model_copy(deep=True)
            next_message = next(normalized_iter, None)
        copied = entry.model_copy(
            update={"message": message, "parent_id": resolved_parent},
            deep=True,
        )
        if copied.parent_id is None and sanitized:
            copied.parent_id = sanitized[-1].id
        elif copied.parent_id is None:
            copied.parent_id = parent_id
        sanitized.append(copied)
        parent_mapping[entry.id] = copied.id
    return sanitized


def _sanitize_conversation_entries(
    entries: builtins.list[ConversationEntry],
) -> builtins.list[ConversationEntry]:
    normalized_entries = [_normalize_conversation_entry(entry) for entry in entries]
    messages = [
        entry.message for entry in normalized_entries if entry.message is not None
    ]
    normalized_messages = normalize_tool_call_sequence(
        messages,
        drop_incomplete_assistant=True,
    )
    tree = SessionTree.import_filtered_transcript(
        normalized_entries,
        normalized_messages,
    )
    return list(tree.entries)


def _normalize_conversation_entry(
    entry: ConversationEntry,
) -> ConversationEntry:
    message = entry.message
    if message is None:
        return entry.model_copy(deep=True)
    normalized = message.model_copy(deep=True)
    if normalized.role == "assistant" and normalized.content is None:
        normalized.content = ""
    return entry.model_copy(update={"message": normalized}, deep=True)
