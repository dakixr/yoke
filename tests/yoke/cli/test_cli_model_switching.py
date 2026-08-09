# ruff: noqa: D100,D101,D102,D103,S101

from __future__ import annotations

from dataclasses import dataclass
from io import StringIO
import json
from pathlib import Path
from threading import RLock
from typing import Never

from yoke.agent.budget import build_provider_context_manager
from yoke.agent.conversation import project_conversation
from yoke.agent.loop import INTERRUPTED_TURN_NOTICE
from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import Message
from yoke.agent.models import TokenUsage
from yoke.agent.state import active_branch_entries
from yoke.agent.state import conversation_entries_from_messages
from yoke.agent.state import transcript_messages_from_entries
from yoke.ai.providers.base import ProviderModelInfo
from yoke.ai.providers.zai import ZAIProvider
from yoke.cli.config import CLIArgs
from yoke.cli.interactive.prompt.cancellation import (
    interrupted_turn_snapshot,
)
from yoke.cli.interactive.model_commands import _switch_model
from yoke.cli.providers.catalog import list_all_provider_model_choices
from yoke.cli.providers.state import set_agent_model
from yoke.cli.providers.state import switch_agent_provider_model
from yoke.cli.render import build_console
from yoke.cli.runtime import persist_session_state
from yoke.cli.runtime import ActiveSession
from yoke.cli.session import SessionRecord
from yoke.cli.session import SessionStore

from .support import active_session_for


@dataclass
class SwitchableConfig:
    model: str = "gpt-a"
    reasoning_effort: str | None = "medium"


class SwitchableProvider:
    provider_name = "demo"
    supports_image_inputs = False
    max_images_per_message = None

    def __init__(self) -> None:
        self.config = SwitchableConfig()
        self.lock = RLock()

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        del messages, tools
        return Message.assistant("done")

    def list_models(self) -> list[ProviderModelInfo]:
        return [
            ProviderModelInfo(
                id="gpt-a",
                display_name="GPT A",
                context_window_tokens=1000,
                thinking_levels=("low", "medium", "high"),
                supports_image_inputs=False,
            ),
            ProviderModelInfo(
                id="gpt-b",
                display_name="GPT B",
                context_window_tokens=2000,
                thinking_levels=("low", "medium", "high"),
                supports_image_inputs=False,
            ),
        ]

    def current_model_id(self) -> str | None:
        return self.config.model

    def current_model_info(self) -> ProviderModelInfo | None:
        for model in self.list_models():
            if model.id == self.config.model:
                return model
        return None

    def set_model(self, model_id: str, *, reasoning_effort: str | None = None) -> None:
        self.config.model = model_id
        if reasoning_effort is not None:
            self.config.reasoning_effort = reasoning_effort


def test_model_switch_large_session_appends_only_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    store = SessionStore(tmp_path / "sessions")
    record = SessionRecord(id="large", root=str(tmp_path))
    store._write_session_record(record)
    path = store._session_path(record.id)
    with path.open("ab") as handle:
        handle.write(b'{"type":"ignored","data":"')
        handle.write(b"x" * (52 * 1024 * 1024))
        handle.write(b'"}\n')
    active_session = ActiveSession(record.id, tmp_path, store, record)
    agent = RuntimeAgent(provider=SwitchableProvider(), tools=[])

    def fail(*_args: object, **_kwargs: object) -> Never:
        raise AssertionError("full session path was used")

    monkeypatch.setattr("yoke.cli.interactive.model_commands.capture_agent_state", fail)
    monkeypatch.setattr(
        "yoke.cli.interactive.model_commands.persist_session_state",
        fail,
    )
    monkeypatch.setattr(SessionStore, "load", fail)
    before = path.stat().st_size

    result = _switch_model(
        "demo:gpt-b",
        "low",
        agent=agent,
        active_session=active_session,
        messages=[],
        console=build_console(StringIO()),
        args=CLIArgs(root=str(tmp_path)),
    )
    appended = path.stat().st_size - before
    with path.open("rb") as handle:
        handle.seek(-min(path.stat().st_size, 1_000), 2)
        last_event = json.loads(handle.read().splitlines()[-1])
    assert result == []
    assert appended < 1_000
    assert last_event["type"] == "metadata"
    assert last_event["model_id"] == "gpt-b"


def test_model_switch_survives_metadata_write_failure(
    tmp_path: Path, monkeypatch
) -> None:
    agent = RuntimeAgent(provider=SwitchableProvider(), tools=[])
    active_session = active_session_for(tmp_path)
    output = StringIO()
    monkeypatch.setattr(
        "yoke.cli.interactive.model_commands.save_active_session_metadata",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    result = _switch_model(
        "demo:gpt-b",
        None,
        agent=agent,
        active_session=active_session,
        messages=[],
        console=build_console(output),
        args=CLIArgs(root=str(tmp_path)),
    )

    assert result == []
    assert isinstance(agent.provider, SwitchableProvider)
    assert agent.provider.config.model == "gpt-b"
    assert "metadata was not saved" in output.getvalue()


def test_same_provider_switch_uses_target_model_default_effort() -> None:
    class AsymmetricProvider(SwitchableProvider):
        def list_models(self) -> list[ProviderModelInfo]:
            return [
                ProviderModelInfo(
                    id="gpt-a",
                    display_name="GPT A",
                    context_window_tokens=1_000,
                    thinking_levels=("low", "medium", "high"),
                    default_thinking_level="medium",
                ),
                ProviderModelInfo(
                    id="gpt-b",
                    display_name="GPT B",
                    context_window_tokens=2_000,
                    thinking_levels=("none", "thinking"),
                    default_thinking_level="thinking",
                ),
            ]

    provider = AsymmetricProvider()
    agent = RuntimeAgent(provider=provider, tools=[])

    state = set_agent_model(agent, model_id="gpt-b", reasoning_effort="medium")

    assert provider.config.model == "gpt-b"
    assert provider.config.reasoning_effort == "thinking"
    assert state.reasoning_effort == "thinking"


def test_same_provider_switch_clears_effort_for_plain_model() -> None:
    class PlainTargetProvider(SwitchableProvider):
        def list_models(self) -> list[ProviderModelInfo]:
            return [
                ProviderModelInfo(
                    id="gpt-a",
                    display_name="GPT A",
                    context_window_tokens=1_000,
                    thinking_levels=("medium",),
                    default_thinking_level="medium",
                ),
                ProviderModelInfo(
                    id="gpt-b",
                    display_name="GPT B",
                    context_window_tokens=2_000,
                ),
            ]

    provider = PlainTargetProvider()
    agent = RuntimeAgent(provider=provider, tools=[])

    state = set_agent_model(agent, model_id="gpt-b")

    assert provider.config.reasoning_effort is None
    assert state.reasoning_effort is None


def test_cross_provider_switch_uses_target_model_default_effort(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ZAI_API_KEY", "test")
    agent = RuntimeAgent(provider=SwitchableProvider(), tools=[])

    state = switch_agent_provider_model(
        agent,
        args=CLIArgs(root=str(tmp_path)),
        qualified_model_id="zai:glm-5.2",
        reasoning_effort="medium",
    )

    assert isinstance(agent.provider, ZAIProvider)
    assert agent.provider.config.reasoning_effort == "thinking"
    assert state.reasoning_effort == "thinking"
    agent.close()


def test_session_store_persists_provider_state(tmp_path: Path) -> None:
    store = SessionStore(directory=tmp_path)

    store.save(
        "demo",
        [Message.user("hello")],
        provider_name="demo",
        model_id="gpt-test",
        reasoning_effort="high",
        context_window_tokens=400_000,
    )

    record = store.load("demo")

    assert record.provider_name == "demo"
    assert record.model_id == "gpt-test"
    assert record.reasoning_effort == "high"
    assert record.context_window_tokens == 400_000


def test_session_store_can_clear_stale_reasoning_effort(tmp_path: Path) -> None:
    store = SessionStore(directory=tmp_path)
    existing = store.save(
        "demo",
        [],
        provider_name="demo",
        model_id="thinking-model",
        reasoning_effort="high",
    )

    updated = store.save(
        "demo",
        [],
        provider_name="demo",
        model_id="plain-model",
        reasoning_effort=None,
        existing_record=existing,
    )

    assert updated.reasoning_effort is None
    assert store.load("demo").reasoning_effort is None


def test_model_state_save_preserves_interrupted_user_correction(
    tmp_path: Path,
) -> None:
    initial_messages = [
        Message.user("remove row grouping"),
        Message.assistant("Row grouping removed."),
    ]
    initial_entries = conversation_entries_from_messages(initial_messages)
    agent = RuntimeAgent(
        provider=SwitchableProvider(),
        tools=[],
        conversation_entries=initial_entries,
    )
    active_session = active_session_for(tmp_path)
    persist_session_state(
        active_session,
        agent,
        initial_messages,
        conversation_entries=initial_entries,
    )
    interrupted_messages, interrupted_entries = interrupted_turn_snapshot(
        messages=initial_messages,
        entries=initial_entries,
        user_message=Message.user("Fix the outcome cell styling."),
    )
    persist_session_state(
        active_session,
        agent,
        interrupted_messages,
        conversation_entries=interrupted_entries,
    )

    persist_session_state(active_session, agent, interrupted_messages)

    record = active_session.store.load(active_session.id)
    branch = active_branch_entries(
        record.conversation_entries,
        leaf_id=record.leaf_id,
    )
    assert [
        message.plain_text_content
        for message in transcript_messages_from_entries(branch)
    ] == [
        "remove row grouping",
        "Row grouping removed.",
        "Fix the outcome cell styling.",
        INTERRUPTED_TURN_NOTICE,
    ]


def test_model_selector_catalog_includes_codex_models(
    tmp_path: Path,
) -> None:
    choices = list_all_provider_model_choices(
        args=CLIArgs(root=str(tmp_path)),
        home=tmp_path,
    )

    qualified_ids = {choice.qualified_id for choice in choices}
    assert "codex:gpt-5.5" in qualified_ids
    assert "codex:gpt-5.6-luna" in qualified_ids
    assert "codex:gpt-5.6-terra" in qualified_ids


def test_model_selector_catalog_includes_open_provider_models(
    tmp_path: Path,
) -> None:
    choices = list_all_provider_model_choices(
        args=CLIArgs(root=str(tmp_path)),
        home=tmp_path,
    )

    qualified_ids = {choice.qualified_id for choice in choices}
    assert "opencode-go:gpt-5.6-luna" in qualified_ids
    assert "opencode-go:kimi-k2.7-code" in qualified_ids
    assert "zai:glm-5.2" in qualified_ids


def test_model_switch_keeps_bounded_user_epoch_for_equal_window() -> None:
    class UsageReportingProvider(SwitchableProvider):
        def __init__(self) -> None:
            super().__init__()
            self.summary_calls = 0

        def list_models(self) -> list[ProviderModelInfo]:
            return [
                ProviderModelInfo(
                    id=model_id,
                    display_name=model_id,
                    context_window_tokens=10_000,
                    thinking_levels=("medium",),
                    supports_image_inputs=False,
                )
                for model_id in ("gpt-a", "gpt-b")
            ]

        def complete(
            self, messages: list[Message], tools: list[dict[str, object]]
        ) -> Message:
            del tools
            if "CONTEXT CHECKPOINT COMPACTION" in str(messages[-1].content):
                self.summary_calls += 1
                return Message.assistant("summary of older work")
            response = Message.assistant("done")
            response.usage = TokenUsage(
                provider_name=self.provider_name,
                model_id=self.config.model,
                input_tokens=4_800,
            )
            return response

    provider = UsageReportingProvider()
    agent = RuntimeAgent(
        provider=provider,
        tools=[],
        context_manager=build_provider_context_manager(
            provider=provider,
            instructions=[],
        ),
        messages=[Message.user("older request " + ("alpha " * 4_000))],
    )
    first_events: list[str] = []
    agent.run(
        "first follow-up",
        on_event=lambda event, _payload: first_events.append(event),
    )
    summary_calls_after_checkpoint = provider.summary_calls

    set_agent_model(agent, model_id="gpt-b")
    second_events: list[str] = []
    result = agent.run(
        "second follow-up",
        on_event=lambda event, _payload: second_events.append(event),
    )

    assert "context_compaction" in first_events
    assert result.output == "done"
    assert provider.summary_calls == summary_calls_after_checkpoint
    assert "context_compaction" not in second_events


def test_model_switch_compacts_for_smaller_target() -> None:
    class ShrinkingProvider(SwitchableProvider):
        def __init__(self) -> None:
            super().__init__()
            self.summary_calls = 0

        def list_models(self) -> list[ProviderModelInfo]:
            return [
                ProviderModelInfo(
                    id="gpt-a",
                    display_name="GPT A",
                    context_window_tokens=10_000,
                ),
                ProviderModelInfo(
                    id="gpt-b",
                    display_name="GPT B",
                    context_window_tokens=1_000,
                ),
            ]

        def complete(
            self, messages: list[Message], tools: list[dict[str, object]]
        ) -> Message:
            del tools
            assert "CONTEXT CHECKPOINT COMPACTION" in str(messages[-1].content)
            self.summary_calls += 1
            return Message.assistant("summary of the oversized history")

    provider = ShrinkingProvider()
    agent = RuntimeAgent(
        provider=provider,
        tools=[],
        context_manager=build_provider_context_manager(
            provider=provider,
            instructions=[],
        ),
        messages=[
            Message.assistant("old output " + ("alpha " * 1_000)),
            Message.user("keep this request"),
        ],
    )

    state = set_agent_model(agent, model_id="gpt-b")

    assert state.model_id == "gpt-b"
    assert provider.summary_calls == 1


def test_interactive_model_switch_adopts_and_persists_compacted_state(
    tmp_path: Path,
) -> None:
    class ShrinkingProvider(SwitchableProvider):
        def list_models(self) -> list[ProviderModelInfo]:
            return [
                ProviderModelInfo(
                    id="gpt-a",
                    display_name="GPT A",
                    context_window_tokens=10_000,
                ),
                ProviderModelInfo(
                    id="gpt-b",
                    display_name="GPT B",
                    context_window_tokens=1_000,
                ),
            ]

        def complete(
            self, messages: list[Message], tools: list[dict[str, object]]
        ) -> Message:
            del tools
            assert "CONTEXT CHECKPOINT COMPACTION" in str(messages[-1].content)
            return Message.assistant("persisted compacted state")

    original_messages = [
        Message.assistant("old output " + ("alpha " * 1_000)),
        Message.user("keep this request"),
    ]
    provider = ShrinkingProvider()
    agent = RuntimeAgent(
        provider=provider,
        tools=[],
        context_manager=build_provider_context_manager(
            provider=provider,
            instructions=[],
        ),
        messages=original_messages,
    )
    active_session = active_session_for(tmp_path)
    persist_session_state(active_session, agent, original_messages)

    updated_messages = _switch_model(
        "demo:gpt-b",
        None,
        agent=agent,
        active_session=active_session,
        messages=original_messages,
        console=build_console(StringIO()),
        args=CLIArgs(root=str(tmp_path)),
    )

    record = active_session.store.load(active_session.id)
    assert updated_messages == agent.messages
    persisted_runtime = project_conversation(
        record.conversation_entries,
        leaf_id=record.leaf_id,
    ).runtime_messages
    assert list(persisted_runtime) == agent.messages
    assert record.messages[:2] == original_messages
    snapshots = [
        entry
        for entry in record.conversation_entries
        if entry.kind == "memory_snapshot"
    ]
    assert len(snapshots) == 1


def test_model_switch_rolls_back_context_when_auto_compaction_fails() -> None:
    class FailingCompactionProvider(SwitchableProvider):
        def list_models(self) -> list[ProviderModelInfo]:
            return [
                ProviderModelInfo(
                    id="gpt-a",
                    display_name="GPT A",
                    context_window_tokens=10_000,
                ),
                ProviderModelInfo(
                    id="gpt-b",
                    display_name="GPT B",
                    context_window_tokens=1_000,
                ),
            ]

        def complete(
            self, messages: list[Message], tools: list[dict[str, object]]
        ) -> Message:
            del messages, tools
            return Message.assistant("")

    provider = FailingCompactionProvider()
    agent = RuntimeAgent(
        provider=provider,
        tools=[],
        context_manager=build_provider_context_manager(
            provider=provider,
            instructions=[],
        ),
        messages=[
            Message.assistant("old output " + ("alpha " * 1_000)),
            Message.user("keep this request"),
        ],
    )
    original_entries = agent.conversation_entries

    try:
        set_agent_model(agent, model_id="gpt-b")
    except ValueError as exc:
        assert "automatic context compaction failed" in str(exc)
    else:
        raise AssertionError("Expected the model switch to fail")

    assert provider.current_model_id() == "gpt-a"
    assert agent.conversation_entries == original_entries
    assert agent.context_manager.compactor.model == "gpt-a"
