"""Tests for the public asynchronous agent facade."""

# ruff: noqa: D101,D102,D103,S101

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import ClassVar

from yoke.agent.models import Message
from yoke.ai import Agent
from yoke.ai import RunConfig
from yoke.ai.providers.base import Provider
from yoke.ai.providers.base import ProviderCancelledError


class ConcurrentProvider(Provider):
    lock: ClassVar[threading.Lock] = threading.Lock()
    active: ClassVar[int] = 0
    maximum_active: ClassVar[int] = 0

    def __init__(self, *, delay: float = 0.02) -> None:
        self.delay = delay

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        del messages, tools
        with self.lock:
            type(self).active += 1
            type(self).maximum_active = max(
                type(self).maximum_active, type(self).active
            )
        try:
            time.sleep(self.delay)
            return Message.assistant("done")
        finally:
            with self.lock:
                type(self).active -= 1


def config(tmp_path: Path) -> RunConfig:
    return RunConfig(root=tmp_path, tools=[], include_agents_file=False)


def test_prompt_async_does_not_block_and_serializes_state(tmp_path: Path) -> None:
    async def scenario() -> None:
        ConcurrentProvider.maximum_active = 0
        agent = Agent(provider=ConcurrentProvider(), config=config(tmp_path))
        ticker = asyncio.create_task(asyncio.sleep(0.005))
        results = await asyncio.gather(
            agent.prompt_async("one"), agent.prompt_async("two"), ticker
        )
        assert results[0].output == "done"
        assert results[1].output == "done"
        assert ConcurrentProvider.maximum_active == 1
        assert [message.content for message in agent.messages] == [
            "one",
            "done",
            "two",
            "done",
        ]
        await agent.aclose()

    asyncio.run(scenario())


def test_prompt_async_timeout_requests_cooperative_stop(tmp_path: Path) -> None:
    class StoppableProvider(Provider):
        stopped = threading.Event()

        def complete(self, messages, tools):
            raise AssertionError("Expected cancellable completion")

        def complete_with_cancel(self, messages, tools, *, cancel_requested):
            del messages, tools
            while not cancel_requested():
                time.sleep(0.002)
            self.stopped.set()
            raise ProviderCancelledError()

    async def scenario() -> None:
        StoppableProvider.stopped.clear()
        agent = Agent(provider=StoppableProvider(), config=config(tmp_path))
        try:
            await agent.prompt_async("wait", timeout=0.01)
        except TimeoutError:
            pass
        else:
            raise AssertionError("Expected timeout")
        assert StoppableProvider.stopped.is_set()
        await agent.aclose()

    asyncio.run(scenario())


def test_agent_supports_async_context_manager_and_default_config(
    tmp_path: Path, monkeypatch
) -> None:
    async def scenario() -> None:
        monkeypatch.chdir(tmp_path)
        agent = Agent(provider=ConcurrentProvider(delay=0))
        async with agent:
            assert agent.root == tmp_path
            assert (await agent.prompt_async("hello")).output == "done"
        assert agent.closed

    asyncio.run(scenario())


def test_prompt_callback_cannot_close_agent(tmp_path: Path) -> None:
    agent = Agent(provider=ConcurrentProvider(delay=0), config=config(tmp_path))

    def on_event(event: str, payload: dict[str, object]) -> None:
        del event, payload
        agent.close()

    try:
        agent.prompt("hello", on_event=on_event)
    except RuntimeError as exc:
        assert "prompt callback" in str(exc)
    else:
        raise AssertionError("Expected callback lifecycle mutation to fail")
    agent.close()
