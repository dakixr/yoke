# ruff: noqa: D100,D101,D102,D103,S101

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import Message
from yoke.ai.providers.base import ProviderModelInfo
from yoke.ai.providers.zai import ZAIProvider
from yoke.cli.config import CLIArgs
from yoke.cli.providers.state import set_agent_model
from yoke.cli.providers.state import switch_agent_provider_model
from yoke.session import SessionStore


from .terminal_daemon_support import terminal as terminal


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
        qualified_model_id="zai:glm-5.3-flash",
        reasoning_effort="medium",
    )

    assert isinstance(agent.provider, ZAIProvider)
    assert agent.provider.config.reasoning_effort == "max"
    assert state.reasoning_effort == "max"
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


class SmallTargetProvider(SwitchableProvider):
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
