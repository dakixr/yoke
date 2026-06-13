"""Conversation tree helpers for CLI session persistence."""

from __future__ import annotations

import builtins
import json
from typing import TYPE_CHECKING

from yoke.agent.message_sanitizer import normalize_tool_call_sequence
from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.agent.state import active_branch_entries
from yoke.agent.state import conversation_entries_from_messages
from yoke.agent.state import merge_conversation_branch
from yoke.agent.state import migrate_conversation_tree

if TYPE_CHECKING:
    from yoke.cli.session import SessionRecord


def _resolve_saved_conversation_tree(
    existing: SessionRecord,
    messages: builtins.list[Message],
    *,
    conversation_entries: builtins.list[ConversationEntry] | None,
    leaf_id: str | None,
) -> tuple[builtins.list[ConversationEntry], str | None]:
    if conversation_entries is not None:
        resolved_entries = _sanitize_conversation_entries(list(conversation_entries))
        leaf_hint = (
            leaf_id
            if leaf_id is not None
            else (resolved_entries[-1].id if resolved_entries else existing.leaf_id)
        )
        return _migrated_conversation_tree(resolved_entries, leaf_hint)
    message_entries = _sanitize_conversation_entries(
        _conversation_entries_for_messages(existing, messages)
    )
    if not existing.conversation_entries:
        return _migrated_conversation_tree(message_entries, leaf_id)
    if not message_entries:
        return _migrated_conversation_tree(
            existing.conversation_entries,
            leaf_id or existing.leaf_id,
        )
    if entries_preserve_active_prefix(existing, message_entries):
        return _migrated_conversation_tree(message_entries, leaf_id)
    merged_entries, merged_leaf_id = merge_conversation_branch(
        existing.conversation_entries,
        message_entries,
    )
    return _migrated_conversation_tree(
        merged_entries,
        leaf_id or merged_leaf_id or existing.leaf_id,
    )


def entries_preserve_active_prefix(
    existing: SessionRecord,
    entries: builtins.list[ConversationEntry],
) -> bool:
    active_entries = active_branch_entries(
        existing.conversation_entries,
        leaf_id=existing.leaf_id,
    )
    if (
        not active_entries
        or len(existing.conversation_entries) != len(active_entries)
        or len(entries) < len(active_entries)
    ):
        return False
    return all(
        entry.message is not None
        and active_entry.message is not None
        and _messages_match(entry.message, active_entry.message)
        for entry, active_entry in zip(entries, active_entries, strict=False)
    )


def _migrated_conversation_tree(
    entries: builtins.list[ConversationEntry],
    leaf_id: str | None,
) -> tuple[builtins.list[ConversationEntry], str | None]:
    resolved_entries, resolved_leaf_id, _tree_changed = migrate_conversation_tree(
        entries,
        leaf_id=leaf_id,
    )
    return resolved_entries, resolved_leaf_id


def _conversation_entries_for_messages(
    existing: SessionRecord,
    messages: builtins.list[Message],
) -> builtins.list[ConversationEntry]:
    message_entries = conversation_entries_from_messages(messages)
    existing_active = (
        active_branch_entries(
            existing.conversation_entries,
            leaf_id=existing.leaf_id,
        )
        or []
    )
    reconciled: builtins.list[ConversationEntry] = []
    parent_id: str | None = None
    for index, entry in enumerate(message_entries):
        existing_entry = (
            existing_active[index] if index < len(existing_active) else None
        )
        if (
            existing_entry is not None
            and entry.message is not None
            and existing_entry.message is not None
            and _messages_match(entry.message, existing_entry.message)
        ):
            copied = existing_entry.model_copy(deep=True)
            parent_id = copied.id
            reconciled.append(copied)
            continue
        copied = entry.model_copy(update={"parent_id": parent_id}, deep=True)
        parent_id = copied.id
        reconciled.append(copied)
    return reconciled


def _messages_match(left: Message, right: Message) -> bool:
    return left.model_dump(mode="json") == right.model_dump(mode="json")


def _sanitize_conversation_entries(
    entries: list[ConversationEntry],
) -> list[ConversationEntry]:
    normalized_entries = [_normalize_conversation_entry(entry) for entry in entries]
    messages = [
        entry.message for entry in normalized_entries if entry.message is not None
    ]
    normalized_messages = normalize_tool_call_sequence(
        messages,
        drop_incomplete_assistant=True,
    )
    normalized_iter = iter(normalized_messages)
    next_message = next(normalized_iter, None)
    sanitized_entries: list[ConversationEntry] = []
    for entry in normalized_entries:
        if entry.message is None:
            sanitized_entries.append(entry.model_copy(deep=True))
            continue
        if next_message is None:
            continue
        current = entry.message.model_copy(deep=True)
        if current == next_message:
            sanitized_entries.append(
                entry.model_copy(
                    update={"message": next_message.model_copy(deep=True)},
                    deep=True,
                )
            )
            next_message = next(normalized_iter, None)
    return sanitized_entries


def _normalize_conversation_entry(
    entry: ConversationEntry,
) -> ConversationEntry:
    message = entry.message
    if message is None:
        return entry.model_copy(deep=True)
    normalized_message = message.model_copy(deep=True)
    if normalized_message.role == "assistant" and normalized_message.content is None:
        normalized_message.content = ""
    return entry.model_copy(update={"message": normalized_message}, deep=True)


def _raw_record_missing_tree_fields(raw_text: str) -> bool:
    try:
        raw = json.loads(raw_text)
    except (ValueError, TypeError):
        return True
    if not isinstance(raw, dict) or "leaf_id" not in raw:
        return True
    entries = raw.get("conversation_entries")
    if not isinstance(entries, list):
        return False
    required = {"id", "parent_id", "created_at"}
    return any(
        isinstance(entry, dict) and not required.issubset(entry) for entry in entries
    )
