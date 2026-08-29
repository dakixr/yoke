"""Shared checkpoint resolution and public projection builders."""

from __future__ import annotations

from dataclasses import dataclass

from yoke.agent.models import ConversationEntry
from yoke.agent.models import MemorySnapshot
from yoke.agent.models import Message
from yoke.agent.tool_context import normalize_legacy_tool_context_entries

from .errors import InvalidCheckpointError
from ._memory import parse_memory_message
from .projections import AuditItem
from .projections import AuditView
from .projections import CheckpointView
from .projections import ConversationView
from .projections import ProviderView
from .projections import RuntimeView
from .projections import RuntimeContextSeed
from .projections import ScrollbackView
from .values import EntryRef
from .values import MessageView
from ._handoff import reconnect_detached_handoff
from ._topology import active_path

_NON_CHAT_KINDS = {
    "instruction",
    "memory_snapshot",
    "compaction_summary",
    "skill_event",
    "control",
    "tool_context",
}
_RUNTIME_HIDDEN_KINDS = {
    "instruction",
    "memory_snapshot",
    "compaction_summary",
}


@dataclass(frozen=True, slots=True)
class _Checkpoint:
    entry: ConversationEntry
    snapshot: MemorySnapshot
    continuation: tuple[ConversationEntry, ...]
    generation: int


def runtime_view(
    entries: list[ConversationEntry], leaf_id: str | None, scope: str
) -> RuntimeView:
    """Build the runtime projection through the shared resolver."""
    path = active_path(entries, leaf_id)
    checkpoint = _resolve_checkpoint(entries, path)
    messages = _provider_messages(path, checkpoint)
    checkpoint_view = None
    if checkpoint is not None:
        mid_turn = bool(checkpoint.snapshot.metadata.get("mid_turn"))
        checkpoint_view = CheckpointView(
            ref=EntryRef(scope, checkpoint.entry.id),
            summary_text=checkpoint.snapshot.summary_text,
            generation=checkpoint.generation,
            mid_turn=mid_turn,
        )
    current = EntryRef(scope, leaf_id) if leaf_id is not None else None
    return RuntimeView(
        current=current,
        messages=tuple(MessageView._from_message(item) for item in messages),
        checkpoint=checkpoint_view,
    )


def provider_view(
    entries: list[ConversationEntry], leaf_id: str | None
) -> ProviderView:
    """Build provider messages with the same checkpoint decision as runtime."""
    path = active_path(entries, leaf_id)
    checkpoint = _resolve_checkpoint(entries, path)
    return ProviderView(
        messages=tuple(
            MessageView._from_message(item)
            for item in _provider_messages(path, checkpoint)
        ),
        checkpoint_generation=(
            checkpoint.generation if checkpoint is not None else None
        ),
    )


def scrollback_view(
    entries: list[ConversationEntry],
    leaf_id: str | None,
    *,
    limit: int,
) -> ScrollbackView:
    """Build scrollback, including detached legacy handoff recovery."""
    path = active_path(entries, leaf_id)
    path, reconnected = reconnect_detached_handoff(entries, path, parse_memory_message)
    messages = [
        entry.message
        for index, entry in enumerate(path)
        if entry.kind not in _NON_CHAT_KINDS
        and entry.message is not None
        and (
            parse_memory_message(entry.message.plain_text_content or "") is None
            or (index == 0 and not reconnected)
        )
    ]
    omitted = max(0, len(messages) - limit)
    visible = messages[omitted:] if limit else []
    notice = None
    if omitted:
        checkpoint = _resolve_checkpoint(entries, path)
        snapshot_note = (
            " Older context is preserved in the session tree."
            if checkpoint is None
            else " Older context is preserved in the session tree and its "
            "compaction summary remains in model context."
        )
        notice = (
            f"Showing the most recent {len(visible):,} of {len(messages):,} "
            f"chat messages; {omitted:,} older messages are hidden from "
            f"startup scrollback.{snapshot_note} Use /tree to inspect earlier "
            "history."
        )
    return ScrollbackView(
        messages=tuple(MessageView._from_message(item) for item in visible),
        omitted_count=omitted,
        notice=notice,
    )


def audit_view(
    entries: list[ConversationEntry], leaf_id: str | None, scope: str
) -> AuditView:
    """Build an iterative preorder layout for all lineages."""
    children: dict[str | None, list[ConversationEntry]] = {}
    for entry in entries:
        children.setdefault(entry.parent_id, []).append(entry)
    active_ids = {entry.id for entry in active_path(entries, leaf_id)}
    roots = children.get(None, [])
    stack: list[tuple[ConversationEntry, int, int, int]] = []
    for lineage, root in reversed(list(enumerate(roots))):
        stack.append((root, 0, lineage, lineage))
    items: list[AuditItem] = []
    while stack:
        entry, depth, lineage, sibling = stack.pop()
        descendants = children.get(entry.id, [])
        label = entry.metadata.get("label")
        items.append(
            AuditItem(
                ref=EntryRef(scope, entry.id),
                kind=entry.kind,
                message=(
                    MessageView._from_message(entry.message)
                    if entry.message is not None
                    else None
                ),
                label=label if isinstance(label, str) else None,
                created_at=entry.created_at,
                depth=depth,
                lineage=lineage,
                sibling_index=sibling,
                child_count=len(descendants),
                on_active_path=entry.id in active_ids,
                current=entry.id == leaf_id,
            )
        )
        for index in range(len(descendants) - 1, -1, -1):
            stack.append((descendants[index], depth + 1, lineage, index))
    return AuditView(items=tuple(items))


def conversation_view(
    entries: list[ConversationEntry], leaf_id: str | None
) -> ConversationView:
    """Build structured views through the shared checkpoint resolver."""
    active = active_path(entries, leaf_id)
    checkpoint = _resolve_checkpoint(entries, active)
    runtime = _runtime_entries(active, entries, checkpoint)
    provider = _provider_messages(active, checkpoint)
    runtime_messages = list(provider)
    snapshot = checkpoint.snapshot if checkpoint is not None else None
    return ConversationView(
        active_entries=tuple(_copy_entries(active)),
        runtime_entries=tuple(_copy_entries(runtime)),
        checkpoint=(snapshot.model_copy(deep=True) if snapshot else None),
        provider_messages=tuple(_copy_messages(provider)),
        runtime_messages=tuple(_copy_messages(runtime_messages)),
    )


def take_runtime_context(
    entries: list[ConversationEntry], leaf_id: str | None
) -> RuntimeContextSeed:
    """Take validated active-path values into one mutable runtime context."""
    normalize_legacy_tool_context_entries(entries)
    selected = leaf_id or (entries[-1].id if entries else None)
    instruction_parents: dict[str, str | None] = {}
    runtime_entries: list[ConversationEntry] = []
    for entry in entries:
        if entry.kind == "instruction":
            instruction_parents[entry.id] = entry.parent_id
            if entry.id == selected:
                selected = entry.parent_id
            continue
        while entry.parent_id in instruction_parents:
            entry.parent_id = instruction_parents[entry.parent_id]
        runtime_entries.append(entry)
    path = active_path(runtime_entries, selected)
    checkpoint = _resolve_checkpoint(runtime_entries, path)
    return RuntimeContextSeed(
        entries=runtime_entries,
        leaf_id=selected,
        messages=_provider_messages(path, checkpoint, defensive=False),
    )


def _runtime_entries(
    active: list[ConversationEntry],
    entries: list[ConversationEntry],
    checkpoint: _Checkpoint | None,
) -> list[ConversationEntry]:
    if checkpoint is None or any(entry.id == checkpoint.entry.id for entry in active):
        return active
    by_id = {entry.id: entry for entry in entries}
    parent = by_id.get(checkpoint.entry.parent_id or "")
    connectors = (
        [parent, checkpoint.entry]
        if parent is not None and parent.kind == "compaction_summary"
        else [checkpoint.entry]
    )
    covered_id = connectors[0].parent_id
    boundary = next(
        index for index, entry in enumerate(active) if entry.id == covered_id
    )
    repaired = _copy_entries(active[: boundary + 1])
    parent_id = repaired[-1].id if repaired else None
    for entry in [*connectors, *active[boundary + 1 :]]:
        copied = entry.model_copy(deep=True)
        copied.parent_id = parent_id
        repaired.append(copied)
        parent_id = copied.id
    return repaired


def _copy_entries(
    entries: list[ConversationEntry],
) -> list[ConversationEntry]:
    return [entry.model_copy(deep=True) for entry in entries]


def _copy_messages(messages: list[Message]) -> list[Message]:
    return [message.model_copy(deep=True) for message in messages]


def _resolve_checkpoint(
    entries: list[ConversationEntry], path: list[ConversationEntry]
) -> _Checkpoint | None:
    positions = {entry.id: index for index, entry in enumerate(path)}
    selected: tuple[int, int, int, _Checkpoint] | None = None
    for order, entry in enumerate(entries):
        if entry.kind != "memory_snapshot":
            continue
        if entry.id not in positions:
            continue
        boundary = positions[entry.id]
        continuation = tuple(path[boundary + 1 :])
        try:
            snapshot = MemorySnapshot.model_validate(entry.metadata)
        except ValueError:
            raise InvalidCheckpointError(
                f"Applicable memory snapshot {entry.id[:8]!r} is invalid."
            ) from None
        generation = _checkpoint_generation(snapshot)
        resolved = _Checkpoint(entry, snapshot, continuation, generation)
        candidate = (boundary, generation, order, resolved)
        if selected is None or candidate[:3] > selected[:3]:
            selected = candidate
    return selected[3] if selected is not None else None


def _provider_messages(
    path: list[ConversationEntry],
    checkpoint: _Checkpoint | None,
    *,
    defensive: bool = True,
) -> list[Message]:
    if checkpoint is None:
        return _normal_messages(path, defensive=defensive)
    checkpoint_index = next(
        index for index, entry in enumerate(path) if entry.id == checkpoint.entry.id
    )
    prior_runtime_instructions = [
        _message_value(entry.message, defensive=defensive)
        for entry in path[:checkpoint_index]
        if entry.kind == "skill_event" and entry.message is not None
    ]
    handoff_state = checkpoint.snapshot.compaction_handoff
    retained_users = (
        handoff_state.retained_messages if handoff_state is not None else []
    )
    prior_users = (
        [_message_value(message, defensive=defensive) for message in retained_users]
        if retained_users
        or (handoff_state is not None and handoff_state.retained_user_messages == 0)
        else [
            _message_value(entry.message, defensive=defensive)
            for entry in path[:checkpoint_index]
            if entry.kind in {"user", "tool_context"}
            and entry.message is not None
            and parse_memory_message(entry.message.plain_text_content or "") is None
        ]
    )
    handoff = (
        _message_value(checkpoint.entry.message, defensive=defensive)
        if checkpoint.entry.message is not None
        else Message.assistant(checkpoint.snapshot.summary_text)
    )
    return [
        *prior_runtime_instructions,
        *prior_users,
        handoff,
        *_normal_messages(path[checkpoint_index + 1 :], defensive=defensive),
    ]


def _normal_messages(
    entries: list[ConversationEntry], *, defensive: bool = True
) -> list[Message]:
    return [
        _message_value(entry.message, defensive=defensive)
        for entry in entries
        if entry.kind not in _RUNTIME_HIDDEN_KINDS and entry.message is not None
    ]


def _message_value(message: Message, *, defensive: bool) -> Message:
    return message.model_copy(deep=True) if defensive else message


def _checkpoint_generation(snapshot: MemorySnapshot) -> int:
    if snapshot.compaction_handoff is not None:
        return snapshot.compaction_handoff.generation
    generation = snapshot.metadata.get("generation", 1)
    return generation if isinstance(generation, int) and generation > 0 else 1
