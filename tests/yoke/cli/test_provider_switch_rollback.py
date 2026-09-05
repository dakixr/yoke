# ruff: noqa: D100, D101, D102, D103, S101

from __future__ import annotations

from dataclasses import dataclass

import pytest

from yoke.agent.budget import build_provider_context_manager
from yoke.agent.budget import rebind_context_manager_budget as real_rebind_budget
from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import Message
from yoke.agent.provider_selection import switch_agent_provider_model
from yoke.ai.providers.base import ProviderModelInfo


@dataclass
class TrackingConfig:
    model: str
    reasoning_effort: str | None = None


class TrackingProvider:
    supports_image_inputs = False
    max_images_per_message = None

    def __init__(
        self,
        *,
        name: str,
        model: str,
        context_window_tokens: int,
        compaction_result: str = "summary",
    ) -> None:
        self.provider_name = name
        self.config = TrackingConfig(model=model)
        self.context_window_tokens = context_window_tokens
        self.compaction_result = compaction_result
        self.close_calls = 0

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        del messages, tools
        return Message.assistant(self.compaction_result)

    def list_models(self) -> list[ProviderModelInfo]:
        return [
            ProviderModelInfo(
                id=self.config.model,
                display_name=self.config.model,
                context_window_tokens=self.context_window_tokens,
            )
        ]

    def current_model_id(self) -> str | None:
        return self.config.model

    def current_model_info(self) -> ProviderModelInfo | None:
        return self.list_models()[0]

    def set_model(self, model_id: str, *, reasoning_effort: str | None = None) -> None:
        self.config.model = model_id
        self.config.reasoning_effort = reasoning_effort

    def close(self) -> None:
        self.close_calls += 1


def _agent(provider: TrackingProvider) -> RuntimeAgent:
    return RuntimeAgent(
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


def test_cross_provider_switch_closes_target_when_prepare_fails(monkeypatch) -> None:
    previous = TrackingProvider(
        name="source",
        model="source-model",
        context_window_tokens=10_000,
        compaction_result="",
    )
    target = TrackingProvider(
        name="target",
        model="target-model",
        context_window_tokens=1_000,
    )
    agent = _agent(previous)
    original_entries = agent.conversation_entries
    monkeypatch.setattr(
        "yoke.agent.provider_selection.build_provider",
        lambda *_args, **_kwargs: target,
    )

    with pytest.raises(ValueError, match="automatic context compaction failed"):
        switch_agent_provider_model(
            agent,
            provider_name="target",
            model_id="target-model",
        )

    assert agent.provider is previous
    assert agent.conversation_entries == original_entries
    assert agent.context_manager.compactor.model == "source-model"
    assert target.close_calls == 1
    assert previous.close_calls == 0


def test_cross_provider_switch_rolls_back_compaction_when_rebind_fails(
    monkeypatch,
) -> None:
    previous = TrackingProvider(
        name="source",
        model="source-model",
        context_window_tokens=10_000,
    )
    target = TrackingProvider(
        name="target",
        model="target-model",
        context_window_tokens=1_000,
    )
    agent = _agent(previous)
    original_entries = agent.conversation_entries
    target_rebinds = 0

    def fail_target_rebind(context_manager, *, provider):
        nonlocal target_rebinds
        budget = real_rebind_budget(context_manager, provider=provider)
        if provider is target:
            target_rebinds += 1
            assert agent.conversation_entries != original_entries
            raise RuntimeError("rebind failed")
        return budget

    monkeypatch.setattr(
        "yoke.agent.provider_selection.build_provider",
        lambda *_args, **_kwargs: target,
    )
    monkeypatch.setattr(
        "yoke.agent.provider_selection.rebind_context_manager_budget",
        fail_target_rebind,
    )

    with pytest.raises(RuntimeError, match="rebind failed"):
        switch_agent_provider_model(
            agent,
            provider_name="target",
            model_id="target-model",
        )

    assert target_rebinds == 1
    assert agent.provider is previous
    assert agent.conversation_entries == original_entries
    assert agent.context_manager.compactor.model == "source-model"
    assert target.close_calls == 1
    assert previous.close_calls == 0
