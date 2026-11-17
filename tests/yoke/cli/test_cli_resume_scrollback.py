from __future__ import annotations

# ruff: noqa: ANN002, ANN003, ANN401, D100, D103, S101


from yoke.agent.conversation import render_memory_message
from yoke.agent.models import Message, ToolCall, ToolFunction
from yoke.cli.render import build_console, print_session_scrollback

from .support import CaptureStream
from .terminal_daemon_support import terminal as terminal

from yoke.agent.models import ConversationEntry
from yoke.agent.models import MemorySnapshot
from yoke.agent.session_tree import (
    SessionTree,
    ConversationProjection,
    ScrollbackProjection,
)
from yoke.session.repository import SessionTreeIndex

from yoke.session.repository import SessionRecord

RESUME_SCROLLBACK_MESSAGE_LIMIT = 400


def test_resume_scrollback_keeps_consecutive_tool_calls_compact() -> None:
    stdout = CaptureStream()
    console = build_console(stdout)
    first_call = ToolCall(
        id="call-1",
        function=ToolFunction(name="read", arguments='{"path":"one"}'),
    )
    second_call = ToolCall(
        id="call-2",
        function=ToolFunction(name="read", arguments='{"path":"two"}'),
    )

    print_session_scrollback(
        console,
        [
            Message.user("inspect"),
            Message(role="assistant", tool_calls=[first_call]),
            Message(role="assistant", tool_calls=[second_call]),
        ],
    )

    assert 'read path="one"\nread path="two"' in stdout.getvalue()


def test_scrollback_handoff_does_not_hide_earlier_tool_activity() -> None:
    stdout = CaptureStream()
    console = build_console(stdout)
    tool_call = ToolCall(
        id="call-1",
        function=ToolFunction(name="read", arguments='{"path":"one"}'),
    )

    print_session_scrollback(
        console,
        [
            Message.user("inspect"),
            Message(role="assistant", tool_calls=[tool_call]),
            Message.tool("call-1", '{"ok": true, "output": "contents"}'),
            Message.assistant("Found it."),
            Message.user(render_memory_message("Internal handoff.")),
            Message.user("continue"),
            Message.assistant("Continuing."),
        ],
    )

    output = stdout.getvalue()
    assert "user inspect" in output
    assert 'read path="one"' in output
    assert "Found it." in output
    assert "Internal handoff." not in output
    assert "user continue" in output
    assert "Continuing." in output


def test_resume_scrollback_hides_handoff_without_hiding_audit_messages() -> None:
    tool_call = ToolCall(
        id="call-1",
        function=ToolFunction(name="read", arguments='{"path":"one"}'),
    )
    messages = [
        Message.user("inspect"),
        Message(role="assistant", content="Checking.", tool_calls=[tool_call]),
        Message.tool("call-1", '{"ok": true, "output": "contents"}'),
        Message.assistant("Found it."),
    ]
    entries: list[ConversationEntry] = []
    parent_id: str | None = None
    for message in messages:
        entry = ConversationEntry(
            kind=(
                "user"
                if message.role == "user"
                else "assistant_tool_calls"
                if message.tool_calls
                else "tool_result"
                if message.role == "tool"
                else "assistant"
            ),
            message=message,
            parent_id=parent_id,
        )
        entries.append(entry)
        parent_id = entry.id
    summary = ConversationEntry(kind="compaction_summary", parent_id=parent_id)
    snapshot = ConversationEntry(
        kind="memory_snapshot",
        parent_id=summary.id,
        metadata=MemorySnapshot(
            id="memory-current", summary_text="Internal handoff."
        ).model_dump(),
    )
    persisted_handoff = ConversationEntry(
        kind="user",
        message=Message.user(render_memory_message("Internal handoff.")),
        parent_id=snapshot.id,
    )
    continuation = ConversationEntry(
        kind="user",
        message=Message.user("continue"),
        parent_id=persisted_handoff.id,
    )
    entries.extend([summary, snapshot, persisted_handoff, continuation])
    record = SessionRecord(
        id="persisted-handoff",
        conversation_entries=entries,
        leaf_id=continuation.id,
    )

    tree = SessionTree.restore(record.conversation_entries, leaf_id=record.leaf_id)
    projection = tree.project(ConversationProjection())
    scrollback = tree.project(
        ScrollbackProjection(limit=RESUME_SCROLLBACK_MESSAGE_LIMIT)
    )
    scrollback_messages = [message.to_message() for message in scrollback.messages]

    assert scrollback_messages == [*messages, continuation.message]
    assert any(
        "Internal handoff." in (message.text_content() or "")
        for message in projection.runtime_messages
    )


def test_resume_scrollback_reconnects_detached_compaction_handoff() -> None:
    historical_user = ConversationEntry(
        kind="user",
        message=Message.user("inspect"),
    )
    historical_agent = ConversationEntry(
        kind="assistant",
        message=Message.assistant("Found it."),
        parent_id=historical_user.id,
    )
    summary = ConversationEntry(
        kind="compaction_summary",
        parent_id=historical_agent.id,
    )
    snapshot = ConversationEntry(
        kind="memory_snapshot",
        parent_id=summary.id,
        metadata=MemorySnapshot(
            id="memory-current",
            summary_text="Detached handoff.",
        ).model_dump(),
    )
    persisted_handoff = ConversationEntry(
        kind="user",
        message=Message.user(render_memory_message("Detached handoff.")),
    )
    continuation = ConversationEntry(
        kind="user",
        message=Message.user("continue"),
        parent_id=persisted_handoff.id,
    )
    record = SessionRecord(
        id="detached-handoff",
        conversation_entries=[
            historical_user,
            historical_agent,
            summary,
            snapshot,
            persisted_handoff,
            continuation,
        ],
        leaf_id=continuation.id,
    )

    tree = SessionTree.restore(record.conversation_entries, leaf_id=record.leaf_id)
    projection = tree.project(ConversationProjection())
    scrollback = tree.project(
        ScrollbackProjection(limit=RESUME_SCROLLBACK_MESSAGE_LIMIT)
    )
    scrollback_messages = [message.to_message() for message in scrollback.messages]

    assert scrollback_messages == [
        historical_user.message,
        historical_agent.message,
        continuation.message,
    ]
    assert list(projection.runtime_messages) == [
        persisted_handoff.message,
        continuation.message,
    ]


def test_resume_recovers_checkpoint_bypassed_by_active_leaf() -> None:
    root = ConversationEntry(kind="user", message=Message.user("old"))
    summary = ConversationEntry(kind="compaction_summary", parent_id=root.id)
    snapshot = ConversationEntry(
        kind="memory_snapshot",
        parent_id=summary.id,
        metadata=MemorySnapshot(
            id="memory-current", summary_text="recovered summary"
        ).model_dump(),
    )
    tail = ConversationEntry(
        kind="user", message=Message.user("tail"), parent_id=root.id
    )
    record = SessionRecord(
        id="orphaned-checkpoint",
        conversation_entries=[root, summary, snapshot, tail],
        leaf_id=tail.id,
    )

    active = SessionTreeIndex(
        record.conversation_entries, record.leaf_id
    ).active_entries()
    tree = SessionTree.restore(active, leaf_id=record.leaf_id)
    projection = tree.project(ConversationProjection())
    scrollback = tree.project(
        ScrollbackProjection(limit=RESUME_SCROLLBACK_MESSAGE_LIMIT)
    )
    [message.to_message() for message in scrollback.messages]

    assert [entry.kind for entry in projection.runtime_entries] == [
        "user",
        "user",
    ]
    assert len(projection.runtime_messages) == 2
    assert projection.runtime_messages[0].text_content() == "old"
    assert projection.runtime_messages[1].text_content() == "tail"
