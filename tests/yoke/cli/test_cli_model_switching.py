# ruff: noqa: D100,D101,D102,D103,S101

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import cast

from .support import CaptureStream
from .support import FakeAgent
from .support import active_session_for
from yoke.agent.compaction import CompactionPreparation
from yoke.agent.compaction import Compactor
from yoke.agent.loop import RuntimeAgent
from yoke.agent.loop import MessageHistory
from yoke.agent.models import Message
from yoke.agent.tools import LocalTool
from yoke.agent.tools import ToolRegistrationContext
from yoke.agent.tools import register_write_tool
from yoke.ai.providers.base import ProviderModelInfo
from yoke.agent.context import ContextManager
from yoke.cli.config import CLIArgs
from yoke.agent.budget import rebind_context_manager_budget
from yoke.cli.interactive.common import handle_slash_command
from yoke.cli.interactive import model_commands
from yoke.cli.providers.state import set_agent_model
from yoke.cli.providers.catalog import ProviderModelChoice
from yoke.cli.providers.catalog import parse_provider_model_identifier
from yoke.cli.render import build_console
from yoke.cli.runtime import apply_session_defaults_to_args
from yoke.cli.session import SessionStore


@dataclass
class SwitchableConfig:
    model: str = "gpt-a"
    reasoning_effort: str | None = "medium"


class SwitchableProvider:
    provider_name = "demo"
    supports_image_inputs = False
    max_images_per_message: int | None = None

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
                system_messages=(Message.system("Use GPT A steering."),),
            ),
            ProviderModelInfo(
                id="gpt-b",
                display_name="GPT B",
                context_window_tokens=2000,
                thinking_levels=("low", "medium", "high"),
                supports_image_inputs=True,
                system_messages=(Message.system("Use GPT B steering."),),
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


def test_session_store_persists_provider_state(tmp_path: Path) -> None:
    store = SessionStore(directory=tmp_path)

    store.save(
        "demo",
        [Message.user("hello")],
        provider_name="codex",
        model_id="gpt-5.4",
        reasoning_effort="high",
        context_window_tokens=400_000,
    )

    record = store.load("demo")

    assert record.provider_name == "codex"
    assert record.model_id == "gpt-5.4"
    assert record.reasoning_effort == "high"
    assert record.context_window_tokens == 400_000


def test_set_agent_model_refreshes_provider_system_messages() -> None:
    provider = SwitchableProvider()
    agent = RuntimeAgent(
        provider=provider,
        tools=[],
        context_manager=ContextManager(
            instructions=[Message.system("Base instructions.")]
        ),
    )

    set_agent_model(agent, model_id="gpt-b")

    contents = [
        message.content
        for message in agent.context_manager.instructions
        if message.role == "system"
    ]
    assert "Base instructions." in contents
    assert "Use GPT B steering." in contents
    assert "Use GPT A steering." not in contents


def test_resume_defaults_provider_state_from_session(tmp_path: Path) -> None:
    store = SessionStore(directory=tmp_path)
    store.save(
        "demo",
        [Message.user("hello")],
        provider_name="codex",
        model_id="gpt-5.4",
        reasoning_effort="high",
        context_window_tokens=400_000,
        root=tmp_path,
    )
    args = CLIArgs(root=str(tmp_path))

    apply_session_defaults_to_args(args, store.load("demo"))

    assert args.model == "codex:gpt-5.4"
    assert args.reasoning_effort == "high"


def test_slash_model_with_legacy_args_only_prints_interactive_usage(
    tmp_path: Path,
) -> None:
    session = active_session_for(tmp_path)
    agent = FakeAgent()
    agent.provider = SwitchableProvider()
    stream = CaptureStream()
    console = build_console(stream)
    messages = [Message.user("hello"), Message.assistant("done")]

    handled, updated_messages, updated_session = handle_slash_command(
        "/model demo:gpt-b high",
        agent=agent,
        active_session=session,
        messages=messages,
        console=console,
    )

    assert handled is True
    assert updated_messages == messages
    assert updated_session is session
    assert updated_session.record.model_id is None
    assert agent.provider.config.model == "gpt-a"
    assert "Usage: /model" in stream.getvalue()


def test_same_provider_switch_rebinds_context_budget(tmp_path: Path) -> None:
    del tmp_path
    agent = FakeAgent()
    agent.provider = SwitchableProvider()
    agent.context_manager = build_context_manager()
    rebind_context_manager_budget(
        agent.context_manager,
        provider=agent.provider,
    )

    state = set_agent_model(agent, model_id="gpt-b", reasoning_effort="high")

    assert state.context_window_tokens == 2000
    assert agent.context_manager.max_total_tokens == 2000
    assert agent.context_manager.compactor.model == "gpt-b"


def test_same_provider_switch_reregisters_model_aware_tools(
    tmp_path: Path,
) -> None:
    registrations: list[str | None] = []

    class ModelTool(LocalTool):
        name = "model_tool"
        description = "Report the model used to register and execute this tool."

        registered_model: str

        def execute(self) -> dict[str, object]:
            return {
                "ok": True,
                "registered_model": self.registered_model,
                "runtime_model": self.context.model_key,
                "provider": self.context.provider,
            }

    def register_tools(context: ToolRegistrationContext):
        registrations.append(context.model_key)
        return [ModelTool(registered_model=context.model_key or "unknown")]

    provider = SwitchableProvider()
    agent = RuntimeAgent(
        provider=provider,
        tools=[],
        tool_factory=register_tools,
        tool_root=tmp_path,
        tool_home=tmp_path,
        context_manager=build_context_manager(),
    )

    set_agent_model(agent, model_id="gpt-b", reasoning_effort="high")
    result = agent.tools["model_tool"].execute()

    assert registrations == ["demo:gpt-a", "demo:gpt-b"]
    assert result == {
        "ok": True,
        "registered_model": "demo:gpt-b",
        "runtime_model": "demo:gpt-b",
        "provider": provider,
    }


def test_same_provider_switch_updates_attach_image_builtin(tmp_path: Path) -> None:
    from yoke.cli.bootstrap.config import resolve_agent_config

    provider = SwitchableProvider()

    def register_cli_tools(context: ToolRegistrationContext):
        resolved = resolve_agent_config(
            root=tmp_path,
            home=tmp_path,
            provider=context.provider,
        )
        return resolved.tools

    agent = RuntimeAgent(
        provider=provider,
        tools=[],
        tool_factory=register_cli_tools,
        tool_root=tmp_path,
        tool_home=tmp_path,
        context_manager=build_context_manager(),
    )

    assert "attach_image" not in agent.tools

    set_agent_model(agent, model_id="gpt-b", reasoning_effort="high")

    assert "attach_image" in agent.tools

    set_agent_model(agent, model_id="gpt-a", reasoning_effort="high")

    assert "attach_image" not in agent.tools


def test_model_switch_changes_builtin_write_interface(tmp_path: Path) -> None:
    class MixedModelProvider(SwitchableProvider):
        def list_models(self) -> list[ProviderModelInfo]:
            return [
                ProviderModelInfo(
                    id="gpt-coder",
                    display_name="GPT Coder",
                    context_window_tokens=2000,
                    thinking_levels=("low", "high"),
                ),
                ProviderModelInfo(
                    id="kimi-code",
                    display_name="Kimi Code",
                    context_window_tokens=2000,
                    thinking_levels=("low", "high"),
                ),
            ]

    provider = MixedModelProvider()
    provider.config.model = "gpt-coder"
    agent = RuntimeAgent(
        provider=provider,
        tools=[],
        tool_factory=register_write_tool,
        tool_root=tmp_path,
        tool_home=tmp_path,
        context_manager=ContextManager(
            instructions=[Message.system("base instructions")]
        ),
    )
    agent.load_conversation(MessageHistory([Message.user("existing conversation")]))

    assert set(agent.tools) == {"apply_patch"}
    assert "Use the `apply_patch` tool" in (
        agent.context_manager.instructions[-1].content or ""
    )

    set_agent_model(agent, model_id="kimi-code")

    assert set(agent.tools) == {"edit"}
    combined = "\n".join(
        str(message.content or "") for message in agent.context_manager.instructions
    )
    assert "base instructions" in combined
    assert "Use the `edit` tool" in combined
    assert "Use the `apply_patch` tool" not in combined
    assert agent._context is not None
    context_combined = "\n".join(
        str(message.content or "") for message in agent._context.instructions
    )
    assert context_combined == combined
    instruction_entries = [
        entry
        for entry in agent._context.conversation_log.entries
        if entry.kind == "instruction"
    ]
    assert [entry.message for entry in instruction_entries] == (
        agent._context.instructions
    )

    set_agent_model(agent, model_id="gpt-coder")

    round_trip = "\n".join(
        str(message.content or "") for message in agent.context_manager.instructions
    )
    assert "Use the `apply_patch` tool" in round_trip
    assert "Use the `edit` tool" not in round_trip
    assert len(agent.context_manager.instructions) == 2


def test_same_provider_switch_does_not_copy_provider(
    tmp_path: Path, monkeypatch
) -> None:
    session = active_session_for(tmp_path)
    agent = RuntimeAgent(
        provider=SwitchableProvider(),
        tools=[],
        context_manager=build_context_manager(),
    )
    rebind_context_manager_budget(
        agent.context_manager,
        provider=agent.provider,
    )
    agent.load_conversation(MessageHistory([Message.user("hello")]))
    stream = CaptureStream()
    console = build_console(stream)

    def select_second_row(rows: list[object], **_kwargs: object) -> object:
        return rows[1]

    def list_demo_choices(**_kwargs: object) -> list[ProviderModelChoice]:
        return [
            ProviderModelChoice(provider_name="demo", model=model)
            for model in cast(SwitchableProvider, agent.provider).list_models()
        ]

    monkeypatch.setattr(
        model_commands,
        "list_all_provider_model_choices",
        list_demo_choices,
    )
    monkeypatch.setattr(
        model_commands,
        "select_table_item_interactive",
        select_second_row,
    )

    handled, _messages, updated_session = handle_slash_command(
        "/model",
        agent=agent,
        active_session=session,
        messages=[Message.user("hello")],
        console=console,
    )

    assert handled is True
    assert updated_session.record.model_id == "gpt-b"
    assert cast(SwitchableProvider, agent.provider).config.model == "gpt-b"


def test_slash_model_switch_preserves_compaction_handoff(
    tmp_path: Path, monkeypatch
) -> None:
    session = active_session_for(tmp_path)
    agent = RuntimeAgent(
        provider=SwitchableProvider(),
        tools=[],
        context_manager=build_context_manager(),
    )
    agent.load_conversation(
        MessageHistory([Message.user("older"), Message.assistant("older answer")])
    )
    preparation = CompactionPreparation(
        reason="manual",
        estimate=Compactor().estimate_tokens(
            agent.messages,
            reserve_tokens=0,
        ),
        boundary="user",
        messages_to_summarize=agent.messages,
        kept_messages=[Message.user("recent")],
        recent_user_messages=[Message.user("recent")],
    )
    assert agent._context is not None
    agent.context_manager.apply_compaction(
        agent._context,
        preparation,
        summary_text="handoff survives model switch",
    )
    session.record.conversation_entries = agent.conversation_entries
    stream = CaptureStream()
    console = build_console(stream)

    def select_second_row(rows: list[object], **_kwargs: object) -> object:
        return rows[1]

    def list_demo_choices(**_kwargs: object) -> list[ProviderModelChoice]:
        return [
            ProviderModelChoice(provider_name="demo", model=model)
            for model in cast(SwitchableProvider, agent.provider).list_models()
        ]

    monkeypatch.setattr(
        model_commands,
        "list_all_provider_model_choices",
        list_demo_choices,
    )
    monkeypatch.setattr(
        model_commands,
        "select_table_item_interactive",
        select_second_row,
    )

    handled, _messages, updated_session = handle_slash_command(
        "/model",
        agent=agent,
        active_session=session,
        messages=agent.messages,
        console=console,
    )

    assert handled is True
    saved = updated_session.store.load(updated_session.id)
    memory_entries = [
        entry for entry in saved.conversation_entries if entry.kind == "memory_snapshot"
    ]
    assert memory_entries
    handoff = cast(dict[str, object], memory_entries[-1].metadata["compaction_handoff"])
    assert handoff["summary_text"] == "handoff survives model switch"
    assert saved.model_id == "gpt-b"


def test_legacy_model_args_do_not_trigger_context_budget_switch(
    tmp_path: Path,
) -> None:
    session = active_session_for(tmp_path)
    agent = RuntimeAgent(
        provider=SwitchableProvider(),
        tools=[],
        context_manager=build_context_manager(),
    )
    rebind_context_manager_budget(
        agent.context_manager,
        provider=agent.provider,
    )
    long_text = "alpha " * 500
    agent.load_conversation(
        MessageHistory(
            [
                Message.user(long_text),
                Message.assistant("done"),
            ]
        )
    )
    stream = CaptureStream()
    console = build_console(stream)
    messages = [Message.user(long_text), Message.assistant("done")]

    handled, updated_messages, updated_session = handle_slash_command(
        "/model demo:gpt-a",
        agent=agent,
        active_session=session,
        messages=messages,
        console=console,
    )

    assert handled is True
    assert updated_messages == messages
    assert updated_session.record.model_id is None
    provider = cast(SwitchableProvider, agent.provider)
    assert provider.config.model == "gpt-a"
    assert "Usage: /model" in stream.getvalue()
    assert "compact before switching" not in stream.getvalue()


def build_context_manager() -> ContextManager:
    from yoke.agent.context import ContextManager

    return ContextManager(instructions=[])


def test_parse_provider_model_identifier() -> None:
    assert parse_provider_model_identifier("Codex:gpt-5.4") == (
        "codex",
        "gpt-5.4",
    )
    assert parse_provider_model_identifier("demo:provider.model-name") == (
        "demo",
        "provider.model-name",
    )
