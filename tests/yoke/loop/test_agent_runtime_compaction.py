from __future__ import annotations

# ruff: noqa: D100, D103, F403, F405, S101

from yoke.agent.compaction import force_compact_agent
from yoke.agent.compaction import CompactionPreparation
from yoke.agent.compaction import TokenEstimate
from yoke.agent.compaction import summary_source_text
from yoke.agent.loop.overflow import should_retry_after_overflow
from yoke.agent.models import MemorySnapshot
from yoke.agent.models import TokenUsage
from yoke.ai.providers.base import ProviderError

from .support import *  # noqa: F403


def test_codex_context_length_error_triggers_overflow_retry() -> None:
    error = ProviderError(
        "Codex WebSocket stream failed: {'error': {'code': "
        "'context_length_exceeded', 'message': 'Your input exceeds the "
        "context window of this model.'}}"
    )

    assert should_retry_after_overflow(error) is True


def test_agent_loop_compacts_and_retries_after_provider_overflow(
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
    provider = OverflowRetryProvider()
    agent = RuntimeAgent(
        provider=provider,
        tools=[],
        context_manager=ContextManager(
            instructions=[Message.system("system prompt")],
            compaction_policy=CompactionPolicy(
                max_total_tokens=800,
                reserved_output_tokens=100,
                keep_recent_tokens=120,
            ),
        ),
        messages=[*older_messages, newest_message],
    )

    result = agent.run("", user_message=newest_message)

    assert result.output == "recovered"
    assert provider.calls == 3


def test_stale_provider_usage_cannot_hide_oversized_resumed_context() -> None:
    class ResumedContextProvider(Provider):
        def __init__(self) -> None:
            self.calls: list[list[Message]] = []

        def complete(
            self, messages: list[Message], tools: list[dict[str, object]]
        ) -> Message:
            del tools
            self.calls.append(messages)
            if messages[-1].content == COMPACTION_SUMMARY_PROMPT:
                return Message.assistant("summary of resumed work")
            return Message.assistant("recovered")

    provider = ResumedContextProvider()
    stale_usage = TokenUsage(input_tokens=10)
    oversized_response = Message.assistant("alpha " * 2_000)
    oversized_response.usage = stale_usage
    agent = RuntimeAgent(
        provider=provider,
        tools=[],
        context_manager=ContextManager(
            compaction_policy=CompactionPolicy(
                max_total_tokens=300,
                reserved_output_tokens=50,
                keep_recent_tokens=40,
                recent_user_tokens=40,
            ),
        ),
        messages=[Message.user("older request"), oversized_response],
    )
    result = agent.run("continue")

    assert result.output == "recovered"
    assert provider.calls[0][0].content == "older request"
    assert provider.calls[0][-1].content == COMPACTION_SUMMARY_PROMPT
    assert all(
        "alpha alpha" not in str(message.content) for message in provider.calls[1]
    )


def test_summary_source_does_not_repeat_retained_user_messages() -> None:
    recent = Message.user("unique retained request")
    preparation = CompactionPreparation(
        reason="forced",
        estimate=TokenEstimate(input_tokens=10, total_with_reserve=10),
        boundary="user",
        messages_to_summarize=[Message.user("older request"), recent],
        kept_messages=[recent.model_copy(deep=True)],
        recent_user_messages=[recent.model_copy(deep=True)],
    )

    source = summary_source_text(preparation)

    assert source.count("unique retained request") == 1
    assert "Recent real user messages" not in source


def test_forced_compaction_updates_runtime_agent_state() -> None:
    class ManualCompactionProvider(Provider):
        def __init__(self) -> None:
            self.calls: list[list[Message]] = []

        def complete(
            self, messages: list[Message], tools: list[dict[str, object]]
        ) -> Message:
            del tools
            self.calls.append([message.model_copy(deep=True) for message in messages])
            if messages[-1].content == COMPACTION_SUMMARY_PROMPT:
                return Message.assistant("manual summary")
            joined = "\n".join(
                str(message.content) for message in messages if message.content
            )
            return Message.assistant(joined)

    provider = ManualCompactionProvider()
    original_messages = [
        Message.user("older request " + ("alpha " * 80)),
        Message.assistant("older response " + ("beta " * 80)),
        Message.user("recent request"),
    ]
    agent = RuntimeAgent(
        provider=provider,
        tools=[],
        context_manager=ContextManager(
            compaction_policy=CompactionPolicy(keep_recent_tokens=1),
        ),
        messages=original_messages,
    )

    compacted = force_compact_agent(agent, agent.messages)

    assert compacted is not None
    assert agent.messages == compacted.messages
    assert [entry.kind for entry in agent.conversation_entries[-2:]] == [
        "compaction_summary",
        "memory_snapshot",
    ]
    assert agent.conversation_entries[-1].kind == "memory_snapshot"
    snapshot = MemorySnapshot.model_validate(agent.conversation_entries[-1].metadata)
    assert snapshot.compaction_handoff is not None
    assert snapshot.compaction_handoff.retained_user_messages == 2
    result = agent.run("next")
    assert "manual summary" in result.output
    assert "older response beta" not in result.output
    assert [message.role for message in compacted.provider_messages] == [
        "user",
        "user",
        "assistant",
    ]
    assert provider.calls[0][:-1] == original_messages
    assert provider.calls[0][-1].content == COMPACTION_SUMMARY_PROMPT


def test_compaction_projection_uses_bounded_recent_user_messages() -> None:
    class BoundedUserProvider(Provider):
        def __init__(self) -> None:
            self.calls: list[list[Message]] = []

        def complete(
            self, messages: list[Message], tools: list[dict[str, object]]
        ) -> Message:
            del tools
            self.calls.append(messages)
            if "CONTEXT CHECKPOINT COMPACTION" in str(messages[-1].content):
                return Message.assistant("bounded handoff")
            return Message.assistant("normal answer")

    provider = BoundedUserProvider()
    original_messages = [
        Message.user("old intent " + ("alpha " * 100)),
        Message.assistant("old work"),
        Message.user("latest intent"),
    ]
    agent = RuntimeAgent(
        provider=provider,
        tools=[],
        context_manager=ContextManager(
            compaction_policy=CompactionPolicy(
                recent_user_tokens=20,
                handoff_target_tokens=10,
            ),
        ),
        messages=original_messages,
    )

    compacted = force_compact_agent(agent, agent.messages)

    assert compacted is not None
    assert "10 estimated tokens" in str(provider.calls[0][-1].content)
    assert [message.content for message in compacted.provider_messages] == [
        "latest intent",
        "bounded handoff",
    ]
    assert any(
        entry.kind == "user"
        and entry.message is not None
        and "old intent" in str(entry.message.content)
        for entry in compacted.conversation_entries
    )
    snapshot = MemorySnapshot.model_validate(
        compacted.conversation_entries[-1].metadata
    )
    assert snapshot.compaction_handoff is not None
    retained = snapshot.compaction_handoff.retained_messages
    assert [message.content for message in retained] == ["latest intent"]


def test_repeated_compaction_keeps_user_intent_and_latest_handoff() -> None:
    class RepeatedCompactionProvider(Provider):
        def __init__(self) -> None:
            self.summary_count = 0

        def complete(
            self, messages: list[Message], tools: list[dict[str, object]]
        ) -> Message:
            del tools
            if messages[-1].content == COMPACTION_SUMMARY_PROMPT:
                self.summary_count += 1
                return Message.assistant(f"handoff {self.summary_count}")
            return Message.assistant("normal answer")

    provider = RepeatedCompactionProvider()
    agent = RuntimeAgent(
        provider=provider,
        tools=[],
        messages=[
            Message.user("user one"),
            Message.assistant("old assistant"),
            Message.user("user two"),
        ],
    )

    first = force_compact_agent(agent, agent.messages)
    assert first is not None
    agent.run("user three")
    second = force_compact_agent(
        agent,
        agent.messages,
        conversation_entries=agent.conversation_entries,
    )

    assert second is not None
    assert [message.content for message in second.provider_messages] == [
        "user one",
        "user two",
        "user three",
        "handoff 2",
    ]
    control_messages = [
        entry.message
        for entry in second.conversation_entries
        if entry.kind == "compaction_summary"
    ]
    assert len(control_messages) == 2
    assert all(
        message is not None and message.content == COMPACTION_SUMMARY_PROMPT
        for message in control_messages
    )


def test_agent_loop_stops_when_epoch_handoff_is_empty(
    tmp_path: Path,
) -> None:
    class EmptySummaryProvider(Provider):
        def complete(
            self, messages: list[Message], tools: list[dict[str, object]]
        ) -> Message:
            if messages[-1].content == COMPACTION_SUMMARY_PROMPT:
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
        messages=[
            Message.user("older"),
            Message.assistant("older answer " + ("alpha " * 200)),
            Message.user("recent"),
            Message.assistant("recent answer"),
        ],
    )

    result = agent.run(
        "follow-up",
        on_event=lambda event, payload: events.append((event, payload)),
    )

    assert result.status == "stopped"
    assert [event for event, _ in events] == [
        "compaction_summary_start",
        "compaction_summary_end",
        "context_compaction_failed",
    ]
    assert any(
        "older answer alpha" in (message.content or "") for message in result.messages
    )
