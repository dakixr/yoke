"""Tests for the public asynchronous agent facade."""

# ruff: noqa: D101,D102,D103,S101

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import ClassVar

from yoke.agent.models import Message
from yoke.agent.models import TokenUsage
from yoke.ai import Agent
from yoke.ai import BatchProgress
from yoke.ai import BatchTask
from yoke.ai import RunConfig
from yoke.ai import run_many
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


def test_run_many_is_bounded_ordered_and_aggregates_usage(
    tmp_path: Path,
) -> None:
    class BatchProvider(Provider):
        lock = threading.Lock()
        active = 0
        maximum_active = 0
        closed = 0

        def __init__(self, output: str, delay: float) -> None:
            self.output = output
            self.delay = delay

        def complete(self, messages, tools):
            del messages, tools
            with self.lock:
                type(self).active += 1
                type(self).maximum_active = max(
                    type(self).maximum_active, type(self).active
                )
            try:
                time.sleep(self.delay)
                return Message.assistant(self.output).model_copy(
                    update={
                        "usage": TokenUsage(
                            input_tokens=2,
                            output_tokens=3,
                            reasoning_tokens=1,
                            total_tokens=6,
                            cached_input_tokens=1,
                        )
                    }
                )
            finally:
                with self.lock:
                    type(self).active -= 1

        def close(self) -> None:
            type(self).closed += 1

    async def scenario() -> None:
        progress: list[BatchProgress] = []
        tasks = [BatchTask(id=str(index), prompt=str(index)) for index in range(5)]

        result = await run_many(
            tasks,
            agent_factory=lambda task: Agent(
                provider=BatchProvider(task.id, delay=0.01 * (5 - int(task.id))),
                config=config(tmp_path),
            ),
            max_concurrency=2,
            on_progress=progress.append,
        )

        assert [item.result.output for item in result.items if item.result] == [
            "0",
            "1",
            "2",
            "3",
            "4",
        ]
        assert BatchProvider.maximum_active == 2
        assert BatchProvider.closed == 5
        assert result.completed_count == 5
        assert result.usage.calls == 5
        assert result.usage.total_tokens == 30
        assert [event.completed for event in progress] == [1, 2, 3, 4, 5]

    asyncio.run(scenario())


def test_run_many_retries_and_rejects_reused_agents(tmp_path: Path) -> None:
    class RetryProvider(Provider):
        def __init__(self, fail: bool) -> None:
            self.fail = fail

        def complete(self, messages, tools):
            del messages, tools
            if self.fail:
                raise RuntimeError("planned")
            return Message.assistant("done")

    async def scenario() -> None:
        attempts = 0

        def factory(task: BatchTask) -> Agent:
            nonlocal attempts
            del task
            attempts += 1
            return Agent(
                provider=RetryProvider(fail=attempts == 1),
                config=config(tmp_path),
            )

        retried = await run_many(
            [BatchTask(id="retry", prompt="retry")],
            agent_factory=factory,
            max_attempts=2,
        )
        assert retried.completed_count == 1
        assert retried.items[0].attempts == 2

        shared = Agent(provider=RetryProvider(fail=False), config=config(tmp_path))
        reused = await run_many(
            [BatchTask(id="one", prompt="one"), BatchTask(id="two", prompt="two")],
            agent_factory=lambda task: shared,
            max_concurrency=1,
        )
        assert reused.items[0].status == "completed"
        assert reused.items[1].status == "error"
        assert "fresh Agent" in str(reused.items[1].error)

    asyncio.run(scenario())


def test_agent_and_forks_share_provider_lifetime(tmp_path: Path) -> None:
    class CloseProvider(Provider):
        closed = 0

        def complete(self, messages, tools):
            del messages, tools
            return Message.assistant("done")

        def close(self) -> None:
            type(self).closed += 1

    CloseProvider.closed = 0
    agent = Agent(provider=CloseProvider(), config=config(tmp_path))
    forked = agent.fork()

    agent.close()
    assert CloseProvider.closed == 0
    assert forked.prompt("hello").output == "done"
    forked.close()
    assert CloseProvider.closed == 1


def test_run_many_marks_timeouts_and_isolates_progress_errors(
    tmp_path: Path,
) -> None:
    class StopProvider(Provider):
        stopped = threading.Event()
        closed = threading.Event()

        def complete(self, messages, tools):
            raise AssertionError("Expected cancellable completion")

        def complete_with_cancel(self, messages, tools, *, cancel_requested):
            del messages, tools
            while not cancel_requested():
                time.sleep(0.002)
            self.stopped.set()
            raise ProviderCancelledError()

        def close(self) -> None:
            self.closed.set()

    async def scenario() -> None:
        StopProvider.stopped.clear()
        StopProvider.closed.clear()

        def broken_progress(progress: BatchProgress) -> None:
            raise RuntimeError(f"progress failed for {progress.task_id}")

        result = await run_many(
            [BatchTask(id="slow", prompt="wait")],
            agent_factory=lambda task: Agent(
                provider=StopProvider(), config=config(tmp_path)
            ),
            timeout=0.01,
            on_progress=broken_progress,
        )

        assert result.items[0].status == "timed_out"
        assert isinstance(result.items[0].error, TimeoutError)
        assert len(result.progress_errors) == 1
        assert StopProvider.stopped.is_set()
        assert StopProvider.closed.is_set()

    asyncio.run(scenario())
