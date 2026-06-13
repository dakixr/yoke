from __future__ import annotations

# ruff: noqa: F403, F405
# ruff: noqa: D100, D103, F403, F405, S101

from yoke.agent.compaction import force_compact_agent
from yoke.agent.loop.overflow import should_retry_after_overflow
from yoke.agent.models import ConversationEntry
from yoke.agent.models import TokenUsage

from .support import *  # noqa: F403, F405


def test_overflow_retry_classifier_does_not_retry_plain_400() -> None:
    assert not should_retry_after_overflow(
        ProviderError("Provider request failed: unsupported parameter", status_code=400)
    )


def test_overflow_retry_classifier_retries_overflow_400() -> None:
    assert should_retry_after_overflow(
        ProviderError(
            "Provider request failed: myokemum context length exceeded",
            status_code=400,
        )
    )


def test_overflow_retry_classifier_retries_413() -> None:
    assert should_retry_after_overflow(
        ProviderError("payload too large", status_code=413)
    )


def test_overflow_retry_classifier_retries_unknown_status_with_phrase() -> None:
    assert should_retry_after_overflow(
        ProviderError("prompt token count exceeds the limit")
    )


def test_agent_loop_retries_after_provider_overflow_by_compacting_history(
    tmp_path: Path,
) -> None:
    older_messages = [
        Message.user("older request"),
        Message.assistant("older response " + ("alpha " * 120)),
    ]
    newest_message = Message.user(
        [
            MessageTextContentPart(text="Describe these images."),
            MessageLocalImageContentPart(
                path=str(tmp_path / "image-1.png"),
                label="[Image #1]",
            ),
        ]
    )
    agent = RuntimeAgent(
        provider=OverflowRetryProvider(),
        tools=[],
        context_manager=ContextManager(
            instructions=[Message.system("system prompt")],
            compaction_policy=CompactionPolicy(
                max_total_tokens=800,
                reserved_output_tokens=100,
                keep_recent_tokens=120,
            ),
        ),
        history=MessageHistory([*older_messages, newest_message]),
    )

    result = agent.run("", user_message=newest_message)

    assert result.output == "recovered"
    assert result.messages[-2].content == newest_message.content
    assert result.messages[-1].content == "recovered"


def test_agent_loop_forces_compaction_after_provider_token_count_overflow(
    tmp_path: Path,
) -> None:
    del tmp_path
    events: list[tuple[str, dict[str, object]]] = []
    newest_message = Message.user("continue")
    agent = RuntimeAgent(
        provider=TokenCountOverflowRetryProvider(),
        tools=[],
        context_manager=ContextManager(
            instructions=[Message.system("system prompt")],
            compaction_policy=CompactionPolicy(
                max_total_tokens=400_000,
                reserved_output_tokens=64_000,
                keep_recent_tokens=2_000,
            ),
        ),
        history=MessageHistory(
            [
                Message.user("older request"),
                Message.assistant("older response"),
                newest_message,
            ]
        ),
    )

    result = agent.run(
        "",
        user_message=newest_message,
        on_event=lambda event, payload: events.append((event, payload)),
    )

    assert result.output == "recovered"
    compaction_events = [
        payload for event, payload in events if event == "context_compaction"
    ]
    assert compaction_events
    assert compaction_events[0]["reason"] == "overflow_retry"


def test_pre_model_compaction_ignores_provider_usage_before_snapshot(
    tmp_path: Path,
) -> None:
    class DoneProvider(Provider):
        supports_image_inputs = True
        max_images_per_message = 50

        def complete(
            self, messages: list[Message], tools: list[dict[str, object]]
        ) -> Message:
            del messages, tools
            return Message.assistant("done")

    del tmp_path
    events: list[tuple[str, dict[str, object]]] = []
    agent = RuntimeAgent(
        provider=DoneProvider(),
        tools=[],
        context_manager=ContextManager(
            instructions=[Message.system("system prompt")],
            compaction_policy=CompactionPolicy(
                max_total_tokens=12_000,
                reserved_output_tokens=1_000,
                keep_recent_tokens=500,
                recent_user_tokens=500,
            ),
        ),
        history=ConversationEntryHistory(
            [
                ConversationEntry(kind="user", message=Message.user("older")),
                ConversationEntry(
                    kind="assistant",
                    message=Message(
                        role="assistant",
                        content="older response",
                        usage=TokenUsage(input_tokens=75_000),
                    ),
                ),
                ConversationEntry(kind="memory_snapshot"),
                ConversationEntry(kind="user", message=Message.user("recent")),
            ]
        ),
    )

    result = agent.run(
        "",
        on_event=lambda event, payload: events.append((event, payload)),
    )

    assert result.output == "done"
    assert not [event for event, _payload in events if event == "context_compaction"]


def test_forced_compaction_updates_runtime_agent_state() -> None:
    class ManualCompactionProvider(Provider):
        def complete(
            self, messages: list[Message], tools: list[dict[str, object]]
        ) -> Message:
            del tools
            if messages[0].content == COMPACTION_SUMMARY_PROMPT:
                return Message.assistant("manual summary")
            joined = "\n".join(
                str(message.content) for message in messages if message.content
            )
            return Message.assistant(joined)

    agent = RuntimeAgent(
        provider=ManualCompactionProvider(),
        tools=[],
        context_manager=ContextManager(
            compaction_policy=CompactionPolicy(keep_recent_tokens=1),
        ),
        history=MessageHistory(
            [
                Message.user("older request " + ("alpha " * 80)),
                Message.assistant("older response " + ("beta " * 80)),
                Message.user("recent request"),
            ]
        ),
    )

    compacted = force_compact_agent(agent, agent.messages)

    assert compacted is not None
    assert agent.messages == compacted.messages
    assert [entry.kind for entry in agent.conversation_entries[-2:]] == [
        "compaction_summary",
        "memory_snapshot",
    ]
    assert agent.conversation_entries[-1].kind == "memory_snapshot"
    result = agent.run("next")
    assert "manual summary" in result.output
    assert "older response beta" not in result.output


def test_agent_loop_stops_when_compaction_summary_is_empty(
    tmp_path: Path,
) -> None:
    class EmptySummaryProvider(Provider):
        def complete(
            self, messages: list[Message], tools: list[dict[str, object]]
        ) -> Message:
            if messages[0].content == COMPACTION_SUMMARY_PROMPT:
                return Message.assistant("")
            return Message.assistant("should not run")

    events: list[tuple[str, dict[str, object]]] = []
    agent = RuntimeAgent(
        provider=EmptySummaryProvider(),
        tools=[],
        context_manager=ContextManager(
            compaction_policy=CompactionPolicy(
                max_total_tokens=300,
                keep_recent_tokens=30,
            ),
        ),
        history=MessageHistory(
            [
                Message.user("older"),
                Message.assistant("older answer " + ("alpha " * 200)),
                Message.user("recent"),
                Message.assistant("recent answer"),
            ]
        ),
    )

    result = agent.run(
        "follow-up",
        on_event=lambda event, payload: events.append((event, payload)),
    )

    assert result.status == "stopped"
    assert [event for event, _payload in events] == [
        "compaction_summary_start",
        "compaction_summary_end",
        "context_compaction_failed",
    ]
    assert result.messages[-1].content == INTERRUPTED_TURN_NOTICE
    assert any(
        "older answer alpha" in (message.content or "") for message in result.messages
    )
