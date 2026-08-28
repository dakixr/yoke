# ruff: noqa: D100, D101, D102, D103, S101

from pathlib import Path

from yoke.agent.compaction import COMPACTION_SUMMARY_PROMPT
from yoke.agent.compaction import CompactionPolicy
from yoke.agent.compaction import force_compact_agent
from yoke.agent.context import ContextManager
from yoke.agent.loop import RuntimeAgent
from yoke.agent.multimodal import messages_for_provider_capabilities
from yoke.agent.models import Message
from yoke.agent.models import MessageLocalImageContentPart
from yoke.agent.models import MessageTextContentPart
from yoke.ai.providers.base import emit_provider_event
from yoke.ai.providers.base import ProviderRequestContext


class ContextRecordingProvider:
    supports_image_inputs = False
    max_images_per_message = None

    def __init__(self) -> None:
        self.cache_scopes: list[str] = []

    def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
    ) -> Message:
        raise AssertionError("The runtime should supply provider context")

    def complete_with_context(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
        *,
        request_context: ProviderRequestContext,
    ) -> Message:
        del messages, tools
        self.cache_scopes.append(request_context.cache_scope)
        return Message.assistant("ok")


def test_runtime_reuses_cache_scope_and_isolates_conversations() -> None:
    first_provider = ContextRecordingProvider()
    first_agent = RuntimeAgent(provider=first_provider, tools=[])

    first_agent.run("same opening prompt")
    first_agent.run("second turn")

    second_provider = ContextRecordingProvider()
    second_agent = RuntimeAgent(provider=second_provider, tools=[])
    second_agent.run("same opening prompt")

    assert first_provider.cache_scopes[0] == first_provider.cache_scopes[1]
    assert first_provider.cache_scopes[0] != second_provider.cache_scopes[0]


def test_compaction_reuses_scope_then_resets_reduced_epoch() -> None:
    class CompactionContextProvider(ContextRecordingProvider):
        def __init__(self) -> None:
            super().__init__()
            self.request_contexts: list[ProviderRequestContext] = []
            self.requests: list[list[Message]] = []
            self.tool_sets: list[list[dict[str, object]]] = []

        def complete_with_context(
            self,
            messages: list[Message],
            tools: list[dict[str, object]],
            *,
            request_context: ProviderRequestContext,
        ) -> Message:
            self.cache_scopes.append(request_context.cache_scope)
            self.request_contexts.append(request_context)
            self.requests.append(messages)
            self.tool_sets.append(tools)
            if messages[-1].content == COMPACTION_SUMMARY_PROMPT:
                return Message.assistant("compacted history")
            return Message.assistant("done")

    provider = CompactionContextProvider()
    initial_messages = [
        Message.user("old request"),
        Message.assistant("old response " + ("alpha " * 200)),
        Message.user("recent request"),
    ]
    agent = RuntimeAgent(
        provider=provider,
        tools=[],
        context_manager=ContextManager(
            compaction_policy=CompactionPolicy(
                max_total_tokens=300,
                reserved_output_tokens=50,
                keep_recent_tokens=30,
            )
        ),
        messages=initial_messages,
    )

    compacted = force_compact_agent(agent, agent.messages)
    assert compacted is not None
    result = agent.run("follow up")

    assert result.output == "done"
    summary_context = provider.request_contexts[0]
    model_context = provider.request_contexts[-1]
    assert summary_context.response_continuity == "continue"
    assert model_context.response_continuity == "reset"
    assert summary_context.cache_scope == model_context.cache_scope
    assert provider.requests[0][:-1] == initial_messages
    assert provider.requests[0][-1].content == COMPACTION_SUMMARY_PROMPT
    assert provider.tool_sets[0] == provider.tool_sets[-1]
    assert [message.role for message in provider.requests[-1]] == [
        "user",
        "user",
        "assistant",
        "user",
    ]


def test_compaction_handoff_uses_the_normal_provider_capability_projection(
    tmp_path: Path,
) -> None:
    class CapabilityRecordingProvider(ContextRecordingProvider):
        supports_image_inputs = False
        max_images_per_message = None

        def __init__(self) -> None:
            super().__init__()
            self.requests: list[list[Message]] = []

        def complete_with_context(
            self,
            messages: list[Message],
            tools: list[dict[str, object]],
            *,
            request_context: ProviderRequestContext,
        ) -> Message:
            del tools
            self.requests.append(messages)
            self.cache_scopes.append(request_context.cache_scope)
            return Message.assistant("image-aware handoff")

    provider = CapabilityRecordingProvider()
    initial_messages = [
        Message.user(
            [
                MessageTextContentPart(text="inspect the image"),
                MessageLocalImageContentPart(
                    path=str(tmp_path / "input.png"),
                    label="[Image #1]",
                ),
            ]
        ),
        Message.assistant("inspection result"),
    ]
    agent = RuntimeAgent(
        provider=provider,
        tools=[],
        messages=initial_messages,
    )

    compacted = force_compact_agent(agent, agent.messages)

    assert compacted is not None
    expected_prefix = messages_for_provider_capabilities(
        initial_messages,
        provider,
    )
    assert provider.requests[0][:-1] == expected_prefix
    assert provider.requests[0][-1].content == COMPACTION_SUMMARY_PROMPT


class EventEmittingProvider:
    supports_image_inputs = False
    max_images_per_message = None

    def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
    ) -> Message:
        del messages, tools
        emit_provider_event(
            "provider_retry",
            {
                "provider": "test",
                "model": "fake",
                "attempt": 1,
                "wait_seconds": 0.0,
                "message": "transient test event",
            },
        )
        return Message.assistant("ok")


def test_runtime_forwards_provider_events() -> None:
    provider = EventEmittingProvider()
    agent = RuntimeAgent(provider=provider, tools=[])
    events: list[tuple[str, dict[str, object]]] = []

    agent.run(
        "trigger",
        on_event=lambda event, payload: events.append((event, payload)),
    )

    assert (
        "provider_retry",
        {
            "provider": "test",
            "model": "fake",
            "attempt": 1,
            "wait_seconds": 0.0,
            "message": "transient test event",
        },
    ) in events


def test_compaction_provider_mutation_isolated_from_agent_history() -> None:
    class MutatingCompactionProvider(ContextRecordingProvider):
        supports_image_inputs = True

        def complete_with_context(
            self,
            messages: list[Message],
            tools: list[dict[str, object]],
            *,
            request_context: ProviderRequestContext,
        ) -> Message:
            del tools, request_context
            messages[0].content = "provider mutation"
            return Message.assistant("safe summary")

    provider = MutatingCompactionProvider()
    original_messages = [
        Message.user("immutable old request"),
        Message.assistant("immutable old answer"),
    ]
    agent = RuntimeAgent(provider=provider, tools=[], messages=original_messages)

    compacted = force_compact_agent(agent, agent.messages)

    assert compacted is not None
    assert original_messages[0].content == "immutable old request"
    assert agent.conversation_entries[0].message is not None
    assert agent.conversation_entries[0].message.content == "immutable old request"
