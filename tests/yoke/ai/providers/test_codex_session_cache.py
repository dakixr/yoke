from __future__ import annotations

# ruff: noqa: D100,D101,D103,S101

from pathlib import Path

from yoke.agent.context import ContextManager
from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import Message
from yoke.ai.providers.base import Provider
from yoke.ai.providers.codex.subscription import (
    register_provider as register_subscription_provider,
)
from yoke.ai.providers.codex.websockets import register_provider


class ProviderContext:
    env: dict[str, str] = {}
    model = "gpt-5.4"
    reasoning_effort = None
    session_id = "session-123"

    def __init__(self, home: Path) -> None:
        self.home = home


def test_codex_provider_cache_key_is_sticky_across_session_resume(
    tmp_path: Path,
) -> None:
    context = ProviderContext(tmp_path)
    first = register_subscription_provider(context)
    resumed = register_subscription_provider(context)
    other_context = ProviderContext(tmp_path)
    other_context.session_id = "session-456"
    other = register_subscription_provider(other_context)
    try:
        assert first._prompt_cache_key == resumed._prompt_cache_key
        assert first._prompt_cache_key != other._prompt_cache_key
    finally:
        first.close()
        resumed.close()
        other.close()


def test_codex_websockets_uses_context_session_for_cache_key(tmp_path: Path) -> None:
    context = ProviderContext(tmp_path)
    first = register_provider(context)
    resumed = register_provider(context)
    try:
        assert first._prompt_cache_key == resumed._prompt_cache_key
    finally:
        first.close()
        resumed.close()


def test_codex_turn_state_resets_between_logical_user_turns(tmp_path: Path) -> None:
    provider = register_provider(ProviderContext(tmp_path))
    provider._turn_state = "turn-123"
    provider._last_response_id = "response-123"

    provider.start_turn()

    assert provider._turn_state is None
    assert provider._last_response_id == "response-123"
    provider.close()


def test_runtime_starts_one_provider_turn_per_run() -> None:
    class TurnAwareProvider(Provider):
        starts = 0

        def start_turn(self) -> None:
            self.starts += 1

        def complete(
            self, messages: list[Message], tools: list[dict[str, object]]
        ) -> Message:
            del messages, tools
            return Message.assistant("done")

    provider = TurnAwareProvider()
    agent = RuntimeAgent(
        provider=provider,
        tools=[],
        context_manager=ContextManager(instructions=[Message.system("system")]),
    )

    agent.run("one")
    agent.run("two")

    assert provider.starts == 2
    agent.close()
