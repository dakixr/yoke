from __future__ import annotations

# ruff: noqa: D100, D103, S101

from pathlib import Path

from yoke.agent.context import ContextManager
from yoke.agent.conversation import project_conversation
from yoke.agent.models import ConversationEntry
from yoke.agent.models import ConversationEntryKind
from yoke.agent.models import MemorySnapshot
from yoke.agent.models import Message
from yoke.cli.session import SessionStore


def _entry(
    kind: ConversationEntryKind,
    *,
    message: Message | None = None,
    parent: ConversationEntry | None = None,
    metadata: dict[str, object] | None = None,
) -> ConversationEntry:
    return ConversationEntry(
        kind=kind,
        message=message,
        parent_id=parent.id if parent else None,
        metadata=metadata or {},
    )


def test_projection_does_not_leak_checkpoint_from_detached_branch() -> None:
    old = _entry("user", message=Message.user("old context"))
    summary = _entry("compaction_summary", parent=old)
    snapshot = _entry(
        "memory_snapshot",
        parent=summary,
        metadata=MemorySnapshot(
            id="memory-current",
            summary_text="authoritative summary",
        ).model_dump(),
    )
    tail = _entry(
        "assistant",
        message=Message.assistant("active tail"),
        parent=old,
    )

    projection = project_conversation([old, summary, snapshot, tail], leaf_id=tail.id)

    assert projection.checkpoint is None
    assert [entry.id for entry in projection.active_entries] == [
        old.id,
        tail.id,
    ]
    assert [entry.id for entry in projection.runtime_entries] == [
        old.id,
        tail.id,
    ]
    assert [message.text_content() for message in projection.provider_messages] == [
        "old context",
        "active tail",
    ]


def test_provider_ignores_detached_checkpoint_on_resume() -> None:
    old = _entry("user", message=Message.user("must not be sent"))
    summary = _entry("compaction_summary", parent=old)
    snapshot = _entry(
        "memory_snapshot",
        parent=summary,
        metadata=MemorySnapshot(
            id="memory-current",
            summary_text="compact state",
        ).model_dump(),
    )
    tail = _entry("user", message=Message.user("continue"), parent=old)
    projection = project_conversation([old, summary, snapshot, tail], leaf_id=tail.id)
    manager = ContextManager()
    context = manager.initialize(
        "",
        conversation_entries=projection.runtime_entries,
        append_prompt=False,
    )

    provider_text = [
        message.text_content() for message in manager.messages_for_provider(context)
    ]

    assert not any("compact state" in (text or "") for text in provider_text)
    assert "continue" in provider_text
    assert "must not be sent" in provider_text


def test_projection_ignores_checkpoints_outside_active_branch() -> None:
    first = _entry("user", message=Message.user("first"))
    first_summary = _entry("compaction_summary", parent=first)
    first_snapshot = _entry(
        "memory_snapshot",
        parent=first_summary,
        metadata=MemorySnapshot(
            id="memory-current", summary_text="first summary"
        ).model_dump(),
    )
    second = _entry("assistant", message=Message.assistant("second"), parent=first)
    second_summary = _entry("compaction_summary", parent=second)
    second_snapshot = _entry(
        "memory_snapshot",
        parent=second_summary,
        metadata=MemorySnapshot(
            id="memory-current", summary_text="second summary"
        ).model_dump(),
    )
    tail = _entry("user", message=Message.user("tail"), parent=second)

    projection = project_conversation(
        [
            first,
            first_summary,
            first_snapshot,
            second,
            second_summary,
            second_snapshot,
            tail,
        ],
        leaf_id=tail.id,
    )

    assert projection.checkpoint is None
    assert [message.text_content() for message in projection.provider_messages] == [
        "first",
        "second",
        "tail",
    ]


def test_projection_ignores_invalid_checkpoint_on_detached_branch() -> None:
    root = _entry("user", message=Message.user("large history"))
    invalid_snapshot = _entry(
        "memory_snapshot",
        parent=root,
        metadata={"summary_text": "missing required id"},
    )
    tail = _entry("user", message=Message.user("tail"), parent=root)

    projection = project_conversation([root, invalid_snapshot, tail], leaf_id=tail.id)
    assert projection.checkpoint is None


def test_persisted_checkpoint_recovery_preserves_original_branch(
    tmp_path: Path,
) -> None:
    root = _entry("user", message=Message.user("old"))
    summary = _entry("compaction_summary", parent=root)
    snapshot = _entry(
        "memory_snapshot",
        parent=summary,
        metadata=MemorySnapshot(
            id="memory-current", summary_text="summary"
        ).model_dump(),
    )
    original_tail = _entry("user", message=Message.user("tail"), parent=root)
    store = SessionStore(directory=tmp_path)
    store.save(
        "session",
        [],
        conversation_entries=[root, summary, snapshot, original_tail],
        leaf_id=original_tail.id,
    )
    record = store.load("session")
    runtime = list(
        project_conversation(
            record.conversation_entries, leaf_id=record.leaf_id
        ).runtime_entries
    )
    new_entry = _entry(
        "assistant", message=Message.assistant("new"), parent=runtime[-1]
    )

    store.save(
        "session",
        [],
        conversation_entries=[*runtime, new_entry],
        existing_record=record,
    )

    saved = store.load("session")
    preserved = next(
        entry for entry in saved.conversation_entries if entry.id == original_tail.id
    )
    active = project_conversation(
        saved.conversation_entries, leaf_id=saved.leaf_id
    ).active_entries
    assert preserved.parent_id == root.id
    assert original_tail.id in {entry.id for entry in active}
    assert [
        entry.message.text_content() for entry in active if entry.message is not None
    ] == ["old", "tail", "new"]


def test_checkpoint_recovery_copies_all_existing_descendants(
    tmp_path: Path,
) -> None:
    root = _entry("user", message=Message.user("old"))
    summary = _entry("compaction_summary", parent=root)
    snapshot = _entry(
        "memory_snapshot",
        parent=summary,
        metadata=MemorySnapshot(
            id="memory-current", summary_text="summary"
        ).model_dump(),
    )
    first = _entry("user", message=Message.user("first"), parent=root)
    second = _entry("assistant", message=Message.assistant("second"), parent=first)
    store = SessionStore(directory=tmp_path)
    store.save(
        "session",
        [],
        conversation_entries=[root, summary, snapshot, first, second],
        leaf_id=second.id,
    )
    record = store.load("session")
    runtime = project_conversation(
        record.conversation_entries, leaf_id=record.leaf_id
    ).runtime_entries

    store.save(
        "session",
        [],
        conversation_entries=list(runtime),
        existing_record=record,
    )

    saved = store.load("session")
    active = project_conversation(
        saved.conversation_entries, leaf_id=saved.leaf_id
    ).active_entries
    active_ids = {entry.id for entry in active}
    assert first.id in active_ids
    assert second.id in active_ids
    assert [
        entry.message.text_content() for entry in active if entry.message is not None
    ] == ["old", "first", "second"]
