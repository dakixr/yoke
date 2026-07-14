from __future__ import annotations

# ruff: noqa: ANN202, D100, D101, D102, D103, S101

import threading
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from typing import cast

from yoke.agent.loop.tools.core import execute_tool
from yoke.agent.models import Message
from yoke.agent.tools import ModelIdentity
from yoke.agent.tools import ToolRuntimeContext
from yoke.agent.tools.web import WebResearchTool
from yoke.ai.providers.base import ProviderCancelledError
from yoke.ai.providers.codex.subscription import CodexSubscriptionProvider


class BlockingHostedSearchProvider(CodexSubscriptionProvider):
    def __init__(self) -> None:
        self.config = cast(Any, SimpleNamespace(timeout_seconds=600.0))
        self.started = threading.Event()
        self.release = threading.Event()
        self.cancel_seen = False

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        del messages, tools
        self.started.set()
        self.release.wait(timeout=5)
        return Message.assistant("late hosted result")

    def complete_with_cancel(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
        *,
        cancel_requested: Callable[[], bool],
    ) -> Message:
        del messages, tools
        self.started.set()
        while not cancel_requested():
            time.sleep(0.01)
        self.cancel_seen = True
        raise ProviderCancelledError()


def test_web_research_cancels_in_flight_hosted_search() -> None:
    stop_event = threading.Event()
    provider = BlockingHostedSearchProvider()
    tool = WebResearchTool.bind()
    tool.bind_runtime_context(
        ToolRuntimeContext(
            root=Path.cwd(),
            home=Path.home(),
            provider=provider,
            model=ModelIdentity(provider_name="codex", model_id="test-model"),
        )
    )
    result: list[dict[str, object]] = []

    worker = threading.Thread(
        target=lambda: result.append(
            execute_tool(
                {tool.name: tool},
                tool.name,
                {"question": "What changed?"},
                cancel_requested=stop_event.is_set,
            )
        ),
        daemon=True,
    )
    worker.start()
    try:
        assert provider.started.wait(timeout=1)
        stop_event.set()
        worker.join(timeout=1)
        assert not worker.is_alive()
    finally:
        provider.release.set()
        worker.join(timeout=1)

    assert provider.cancel_seen is True
    assert result and result[0]["ok"] is False
