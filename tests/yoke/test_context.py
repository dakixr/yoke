# ruff: noqa: D100,D103,E501,S101

from __future__ import annotations

from yoke.agent.compaction import CompactionPolicy
from yoke.agent.context import ContextManager
from yoke.ai.providers.openai_compat import (
    normalize_openai_request_messages,
)
from yoke.agent.models import Message, ToolCall, ToolFunction


def test_context_manager_prepare_compaction_rebuilds_checkpoint() -> None:
    manager = ContextManager(
        compaction_policy=CompactionPolicy(
            max_total_tokens=250,
            reserved_output_tokens=83,
            keep_recent_tokens=50,
        )
    )
    context = manager.initialize(
        "follow-up",
        [
            Message.user("older"),
            Message.assistant("done"),
            Message.user("big request"),
            Message.assistant("prefix " + ("alpha " * 200)),
            Message.tool("call-1", '{"ok":true,"stdout":"' + ("beta " * 200) + '"}'),
            Message.assistant("suffix"),
        ],
    )

    preparation = manager.prepare_compaction(context, reason="forced")

    assert preparation is not None
    assert preparation.boundary == "user"
    assert [message.role for message in preparation.messages_to_summarize] == [
        "user",
        "assistant",
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
    ]
    assert [message.role for message in preparation.kept_messages] == [
        "user",
        "user",
        "user",
    ]
    assert preparation.kept_messages[-1].content == "follow-up"


def test_retained_user_messages_obey_strict_token_budget() -> None:
    manager = ContextManager()
    retained = manager.compactor.collect_recent_user_messages(
        [
            Message.user("old intent " + ("alpha " * 100)),
            Message.user("latest intent"),
        ],
        token_budget=32,
    )

    estimate = manager.compactor.estimate_tokens(retained, reserve_tokens=0)

    assert estimate.input_tokens <= 32
    assert retained[-1].content == "latest intent"
    assert "truncated during context compaction" in str(retained[0].content)


def test_normalize_openai_request_messages_drops_invalid_tool_turn_and_tool_calls_on_tool() -> (
    None
):
    messages = [
        Message.user("Start"),
        Message(
            role="assistant",
            content="Attaching previews now.",
            tool_calls=[
                ToolCall(
                    id="call-image-1",
                    function=ToolFunction(
                        name="attach_image",
                        arguments='{"path":"page1.png"}',
                    ),
                ),
                ToolCall(
                    id="call-run",
                    function=ToolFunction(
                        name="python_exec",
                        arguments='{"code":"print(1)"}',
                    ),
                ),
            ],
        ),
        Message(
            role="tool",
            tool_call_id="call-image-1",
            content='{"ok": true}',
            tool_calls=[
                ToolCall(
                    id="illegal-nested-call",
                    function=ToolFunction(
                        name="attach_image",
                        arguments='{"path":"bad.png"}',
                    ),
                )
            ],
        ),
        Message.user("continue"),
    ]

    normalized = normalize_openai_request_messages(messages)

    assert [message.role for message in normalized] == ["user", "user"]
