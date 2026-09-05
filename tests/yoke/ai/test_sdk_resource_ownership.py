from __future__ import annotations

# ruff: noqa: D100, D101, D102, D103, S101

import asyncio
from collections.abc import Callable
from pathlib import Path
import threading
from typing import ClassVar

import pytest

from yoke.agent.loop import in_process_tool as in_process_tool_module
from yoke.agent.loop.in_process_tool import InProcessToolShutdownError
from yoke.agent.loop.in_process_tool import execute_in_process_tool
from yoke.agent.loop import resources as loop_resources
from yoke.agent.models import Message
from yoke.agent.tools import LocalTool
from yoke.ai import Agent
from yoke.ai import RunConfig
from yoke.ai.sdk.resources import CloseAttempt


class TrackingProvider:
    supports_image_inputs: ClassVar[bool] = False
    max_images_per_message: ClassVar[int | None] = None

    def __init__(self) -> None:
        self.close_calls = 0

    def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
    ) -> Message:
        del messages, tools
        return Message.assistant("done")

    def close(self) -> None:
        self.close_calls += 1


class BlockingTool(LocalTool):
    name = "blocking"
    description = "Block until the test releases this in-process tool."
    execute_in_process = True

    def execute(self) -> dict[str, object]:
        started = self._context["started"]
        release = self._context["release"]
        finished = self._context["finished"]
        assert isinstance(started, threading.Event)
        assert isinstance(release, threading.Event)
        assert isinstance(finished, threading.Event)
        started.set()
        release.wait()
        finished.set()
        return {"ok": True}

    def owned_resources(self) -> tuple[object, ...]:
        return (self._context["resource"],)


class TrackingResource:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


def build_blocked_agent(
    tmp_path: Path,
) -> tuple[
    Agent,
    TrackingProvider,
    TrackingResource,
    threading.Event,
    threading.Event,
    threading.Event,
]:
    provider = TrackingProvider()
    resource = TrackingResource()
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    tool = BlockingTool.bind(
        started=started,
        release=release,
        finished=finished,
        resource=resource,
    )
    agent = Agent(
        provider=provider,
        config=RunConfig(
            root=tmp_path,
            tools=[tool],
            include_agents_file=False,
        ),
    )
    result, stopped = execute_in_process_tool(
        tools=agent._runtime.tools,
        name=BlockingTool.name,
        arguments={},
        stop_requested=lambda: True,
    )
    assert stopped is True
    assert result["cancelled"] is True
    return agent, provider, resource, started, release, finished


def test_failed_sdk_close_retains_provider_lease_and_can_be_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        agent, provider, resource, started, release, finished = build_blocked_agent(
            tmp_path
        )
        assert await asyncio.to_thread(started.wait, 1)
        monkeypatch.setattr(
            in_process_tool_module,
            "IN_PROCESS_TOOL_SHUTDOWN_SECONDS",
            0.0,
        )

        with pytest.raises(InProcessToolShutdownError, match="runtime remains open"):
            await agent.aclose()

        assert agent.closed is False
        assert provider.close_calls == 0
        assert resource.close_calls == 0

        release.set()
        assert await asyncio.to_thread(finished.wait, 1)
        await agent.aclose()

        assert agent.closed is True
        assert provider.close_calls == 1
        assert resource.close_calls == 1

    asyncio.run(scenario())


def test_concurrent_aclose_callers_share_failure_then_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        agent, provider, resource, started, release, finished = build_blocked_agent(
            tmp_path
        )
        assert await asyncio.to_thread(started.wait, 1)
        monkeypatch.setattr(
            in_process_tool_module,
            "IN_PROCESS_TOOL_SHUTDOWN_SECONDS",
            0.0,
        )
        close_entered = threading.Event()
        continue_close = threading.Event()
        waiter_entered = threading.Event()
        original_runtime_close = agent._runtime.close
        original_attempt_wait = CloseAttempt.wait

        def blocked_runtime_close() -> None:
            close_entered.set()
            continue_close.wait()
            original_runtime_close()

        def observed_attempt_wait(attempt: CloseAttempt) -> None:
            waiter_entered.set()
            original_attempt_wait(attempt)

        monkeypatch.setattr(agent._runtime, "close", blocked_runtime_close)
        monkeypatch.setattr(CloseAttempt, "wait", observed_attempt_wait)

        first = asyncio.create_task(agent.aclose())
        assert await asyncio.to_thread(close_entered.wait, 1)
        second = asyncio.create_task(agent.aclose())
        assert await asyncio.to_thread(waiter_entered.wait, 1)
        continue_close.set()

        outcomes = await asyncio.gather(first, second, return_exceptions=True)
        assert all(isinstance(item, InProcessToolShutdownError) for item in outcomes)
        assert agent.closed is False
        assert provider.close_calls == 0
        assert resource.close_calls == 0

        release.set()
        assert await asyncio.to_thread(finished.wait, 1)
        await agent.aclose()

        assert agent.closed is True
        assert provider.close_calls == 1
        assert resource.close_calls == 1

    asyncio.run(scenario())


def test_tool_resource_release_closes_every_resource_outside_lease_lock() -> None:
    closed: list[str] = []
    first_error = RuntimeError("first close failed")

    class FailingResource:
        def __init__(self, name: str, error: RuntimeError) -> None:
            self.name = name
            self.error = error

        def close(self) -> None:
            is_owned = getattr(loop_resources._RESOURCE_LEASE_LOCK, "_is_owned")
            assert isinstance(is_owned, Callable)
            assert is_owned() is False
            closed.append(self.name)
            raise self.error

    class ResourceTool(LocalTool):
        name = "resource"
        description = "Own test resources."

        def execute(self) -> dict[str, object]:
            return {"ok": True}

        def owned_resources(self) -> tuple[object, ...]:
            resources = self._context["resources"]
            assert isinstance(resources, tuple)
            return resources

    resources = (
        FailingResource("first", first_error),
        FailingResource("second", RuntimeError("second close failed")),
    )
    tool = ResourceTool.bind(resources=resources)
    loop_resources.acquire_tool_resources([tool])

    with pytest.raises(RuntimeError) as raised:
        loop_resources.release_tool_resources([tool])

    assert raised.value is first_error
    assert closed == ["first", "second"]


def test_failed_close_blocks_new_work_until_cleanup_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = TrackingProvider()
    agent = Agent(
        provider=provider,
        config=RunConfig(root=tmp_path, tools=[], include_agents_file=False),
    )

    def still_running() -> None:
        raise InProcessToolShutdownError("tool still running")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(agent._runtime, "close", still_running)
            with pytest.raises(InProcessToolShutdownError):
                agent.close()
            with pytest.raises(RuntimeError, match="closing or closed"):
                agent.prompt("must not reach the provider")
            with pytest.raises(RuntimeError, match="closing or closed"):
                agent.reset()
            assert provider.close_calls == 0
        agent.close()
        assert agent.closed is True
        assert provider.close_calls == 1
    finally:
        agent.close()


@pytest.mark.parametrize("async_reentry", [False, True])
def test_provider_close_can_reenter_its_owning_agent_without_waiting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, async_reentry: bool
) -> None:
    class ReentrantProvider(TrackingProvider):
        def close(self) -> None:
            super().close()
            if async_reentry:
                asyncio.run(agent.aclose())
            else:
                agent.close()

    provider = ReentrantProvider()
    agent = Agent(
        provider=provider,
        config=RunConfig(root=tmp_path, tools=[], include_agents_file=False),
    )

    def unexpected_wait(_attempt: CloseAttempt) -> None:
        raise AssertionError("Closing callback must not wait for itself")

    monkeypatch.setattr(CloseAttempt, "wait", unexpected_wait)
    try:
        agent.close()
        assert agent.closed is True
        assert provider.close_calls == 1
    finally:
        agent.close()
