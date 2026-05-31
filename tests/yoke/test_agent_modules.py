from __future__ import annotations

from yoke.agent.compaction import (
    CompactionPolicy,
    CompactionPreparation,
    Compactor,
    build_summary_handoff_messages,
)
from yoke.agent.context import ContextManager
from yoke.agent.prompting import parse_memory_message
from yoke.agent.prompting import render_memory_message
from yoke.agent.models import (
    CompactionHandoff,
    ConversationEntry,
    Message,
    ToolCall,
    ToolFunction,
)
from yoke.agent.state import capture_agent_state


def test_memory_message_round_trips() -> None:
    rendered = render_memory_message("remember this")

    assert parse_memory_message(rendered) == "remember this"
    assert parse_memory_message("not a memory message") is None


def test_compactor_should_compact_respects_budget_and_soft_trigger() -> None:
    compactor = Compactor()
    hard_policy = CompactionPolicy(
        max_total_tokens=120,
        reserved_output_tokens=20,
    )
    soft_policy = CompactionPolicy(
        max_total_tokens=200,
        reserved_output_tokens=20,
        soft_trigger_ratio=0.5,
    )

    assert compactor.should_compact(
        compactor.estimate_tokens([Message.user("alpha " * 200)], reserve_tokens=20),
        policy=hard_policy,
    )
    assert compactor.should_compact(
        compactor.estimate_tokens([Message.user("alpha " * 80)], reserve_tokens=20),
        policy=soft_policy,
    )


def test_build_summary_handoff_messages_render_compaction_source() -> None:
    preparation = CompactionPreparation(
        reason="forced",
        estimate=Compactor().estimate_tokens(
            [Message.user("older request")], reserve_tokens=0
        ),
        boundary="split_turn",
        messages_to_summarize=[
            Message.user("older request"),
            Message(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        function=ToolFunction(
                            name="read", arguments='{"path":"README.md"}'
                        ),
                    )
                ],
            ),
        ],
        kept_messages=[Message.tool("call-1", '{"ok":true}')],
        turn_prefix_messages=[Message.user("prefix request")],
    )
    messages = build_summary_handoff_messages(preparation)
    source = messages[1].content or ""

    assert "CONTEXT CHECKPOINT COMPACTION" in (messages[0].content or "")
    assert '[Assistant tool calls] read({"path":"README.md"})' in source
    assert "Current turn prefix" in source
    assert "prefix request" in source
    assert "Recent real user messages that will remain visible" in source


def test_agent_state_capture_prefers_conversation_entries() -> None:
    entries = [
        ConversationEntry(kind="memory_snapshot", metadata={}),
        ConversationEntry(kind="user", message=Message.user("hello")),
    ]

    state = capture_agent_state(object(), conversation_entries=entries)

    assert state.conversation_entries == entries
    assert [message.role for message in state.messages] == ["user"]


def test_compaction_handoff_is_typed_on_memory_snapshot() -> None:
    manager = ContextManager()
    context = manager.initialize(
        "recent",
        [Message.user("older"), Message.assistant("older answer")],
    )
    preparation = CompactionPreparation(
        reason="manual",
        estimate=Compactor().estimate_tokens(context.messages, reserve_tokens=0),
        boundary="user",
        messages_to_summarize=context.messages,
        kept_messages=[Message.user("recent")],
        recent_user_messages=[Message.user("recent")],
    )

    manager.apply_compaction(context, preparation, summary_text="handoff")

    snapshot = context.memory.current_snapshot
    assert snapshot is not None
    assert isinstance(snapshot.compaction_handoff, CompactionHandoff)
    assert snapshot.compaction_handoff.summary_text == "handoff"
    assert snapshot.compaction_handoff.retained_messages == [Message.user("recent")]
