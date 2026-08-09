# ruff: noqa: D100, D101, D102, D103, S101

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
import threading
import time
from typing import ClassVar
from typing import cast

from pydantic import BaseModel

from yoke.agent.models import Message
from yoke.agent.models import TokenUsage
from yoke.ai import Agent
from yoke.ai import BatchProgress
from yoke.ai import BatchTask
from yoke.ai import RunConfig
from yoke.ai import run_many
from yoke.ai.providers.base import ProviderCancelledError


class ConcurrentProvider:
    supports_image_inputs: ClassVar[bool] = False
    max_images_per_message: ClassVar[int | None] = None
    lock: ClassVar[threading.Lock] = threading.Lock()
    active: ClassVar[int] = 0
    maximum_active: ClassVar[int] = 0
    closed: ClassVar[int] = 0

    def __init__(
        self,
        output: str,
        *,
        delay: float = 0.02,
        fail: bool = False,
    ) -> None:
        self.output = output
        self.delay = delay
        self.fail = fail

    @classmethod
    def reset(cls) -> None:
        cls.active = 0
        cls.maximum_active = 0
        cls.closed = 0

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
            if self.fail:
                raise RuntimeError("planned failure")
            return Message.assistant(self.output).model_copy(
                update={
                    "usage": TokenUsage(
                        input_tokens=2,
                        output_tokens=3,
                        reasoning_tokens=1,
                        total_tokens=6,
                        cached_input_tokens=1,
                        cache_creation_input_tokens=2,
                    )
                }
            )
        finally:
            with self.lock:
                type(self).active -= 1

    def close(self) -> None:
        type(self).closed += 1


class CancellableProvider:
    supports_image_inputs: ClassVar[bool] = False
    max_images_per_message: ClassVar[int | None] = None
    stopped = threading.Event()
    closed = threading.Event()

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        raise AssertionError("Expected cancellable completion path")

    def complete_with_cancel(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
        *,
        cancel_requested: Callable[[], bool],
    ) -> Message:
        del messages, tools
        while not cancel_requested():
            time.sleep(0.002)
        self.stopped.set()
        raise ProviderCancelledError()

    def close(self) -> None:
        self.closed.set()


class BlockingProvider:
    supports_image_inputs: ClassVar[bool] = False
    max_images_per_message: ClassVar[int | None] = None

    def __init__(self, *, release_after: float | None = None) -> None:
        self.release_after = release_after
        self.started = threading.Event()
        self.release = threading.Event()

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        del messages, tools
        self.started.set()
        if self.release_after is None:
            self.release.wait()
        else:
            time.sleep(self.release_after)
        return Message.assistant("late")

    def close(self) -> None:
        pass


class SequencedProvider(ConcurrentProvider):
    def __init__(self, outputs: list[str]) -> None:
        super().__init__(outputs[0])
        self.outputs = iter(outputs)

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        self.output = next(self.outputs)
        return super().complete(messages, tools)


class StructuredAnswer(BaseModel):
    value: str


def config(tmp_path: Path) -> RunConfig:
    return RunConfig(root=tmp_path, tools=[], include_agents_file=False)


def test_prompt_async_does_not_block_event_loop(tmp_path: Path) -> None:
    async def scenario() -> None:
        agent = Agent(
            provider=ConcurrentProvider("done", delay=0.04),
            config=config(tmp_path),
        )
        prompt = asyncio.create_task(agent.prompt_async("hello"))
        await asyncio.sleep(0.005)
        assert not prompt.done()
        assert (await prompt).output == "done"
        await agent.aclose()

    asyncio.run(scenario())


def test_prompt_async_timeout_requests_cooperative_stop(tmp_path: Path) -> None:
    async def scenario() -> None:
        CancellableProvider.stopped.clear()
        CancellableProvider.closed.clear()
        agent = Agent(provider=CancellableProvider(), config=config(tmp_path))
        try:
            await agent.prompt_async("wait", timeout=0.01)
        except TimeoutError:
            pass
        else:
            raise AssertionError("Expected timeout")
        async with asyncio.timeout(0.2):
            while not CancellableProvider.stopped.is_set():
                await asyncio.sleep(0.001)
        await agent.aclose()

    asyncio.run(scenario())


def test_prompt_async_timeout_includes_queue_wait(tmp_path: Path) -> None:
    async def scenario() -> None:
        agent = Agent(
            provider=ConcurrentProvider("done", delay=0.04),
            config=config(tmp_path),
        )
        active = asyncio.create_task(agent.prompt_async("active"))
        await asyncio.sleep(0.005)
        try:
            await agent.prompt_async("queued", timeout=0.005)
        except TimeoutError:
            pass
        else:
            raise AssertionError("Expected timeout while waiting in queue")
        assert (await active).output == "done"
        await agent.aclose()

    asyncio.run(scenario())


def test_outer_asyncio_timeout_does_not_wait_for_late_sync_work(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        provider = BlockingProvider(release_after=0.15)
        agent = Agent(provider=provider, config=config(tmp_path))
        prompt = asyncio.create_task(agent.prompt_async("wait"))
        while not provider.started.is_set():
            await asyncio.sleep(0)

        started = time.monotonic()
        try:
            async with asyncio.timeout(0.01):
                await prompt
        except TimeoutError:
            pass
        else:
            raise AssertionError("Expected outer timeout")
        assert time.monotonic() - started < 0.08

        await agent.aclose()

    asyncio.run(scenario())


def test_prompt_async_task_cancellation_does_not_drain_hung_sync_work(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        provider = BlockingProvider()
        agent = Agent(provider=provider, config=config(tmp_path))
        prompt = asyncio.create_task(agent.prompt_async("wait"))
        while not provider.started.is_set():
            await asyncio.sleep(0)

        started = time.monotonic()
        prompt.cancel()
        try:
            await prompt
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("Expected prompt cancellation")
        assert time.monotonic() - started < 0.05

        provider.release.set()
        await agent.aclose()

    asyncio.run(scenario())


def test_prompt_async_parses_structured_output(tmp_path: Path) -> None:
    async def scenario() -> None:
        agent = Agent(
            provider=ConcurrentProvider('{"value":"ok"}'),
            config=config(tmp_path),
        )
        result = await agent.prompt_async("return json", output_type=StructuredAnswer)
        assert result.structured == StructuredAnswer(value="ok")
        await agent.aclose()

    asyncio.run(scenario())


def test_structured_retry_instruction_is_scoped(tmp_path: Path) -> None:
    agent = Agent(
        provider=SequencedProvider(["not-json", '{"value":"ok"}', "plain"]),
        config=config(tmp_path),
    )
    result = agent.prompt("json", output_type=StructuredAnswer)
    assert result.structured == StructuredAnswer(value="ok")
    assert agent.prompt("plain").output == "plain"
    assert agent._runtime._context is not None
    assert all(
        "corrected structured output" not in message.plain_text_content.lower()
        for message in agent._runtime._context.instructions
    )
    agent.close()


def test_prompt_async_serializes_shared_agent_state(tmp_path: Path) -> None:
    async def scenario() -> None:
        ConcurrentProvider.reset()
        agent = Agent(
            provider=ConcurrentProvider("done", delay=0.02),
            config=config(tmp_path),
        )
        await asyncio.gather(agent.prompt_async("one"), agent.prompt_async("two"))
        assert ConcurrentProvider.maximum_active == 1
        assert [message.content for message in agent.messages] == [
            "one",
            "done",
            "two",
            "done",
        ]
        await agent.aclose()

    asyncio.run(scenario())


def test_aclose_waits_for_active_prompt(tmp_path: Path) -> None:
    async def scenario() -> None:
        ConcurrentProvider.reset()
        agent = Agent(
            provider=ConcurrentProvider("done", delay=0.04),
            config=config(tmp_path),
        )
        prompt = asyncio.create_task(agent.prompt_async("hello"))
        await asyncio.sleep(0.005)
        closing = asyncio.create_task(agent.aclose())
        await asyncio.sleep(0.005)
        assert not closing.done()
        assert (await prompt).output == "done"
        await closing
        assert ConcurrentProvider.closed == 1

    asyncio.run(scenario())


def test_aclose_fences_queued_async_prompt(tmp_path: Path) -> None:
    async def scenario() -> None:
        agent = Agent(
            provider=ConcurrentProvider("done", delay=0.04),
            config=config(tmp_path),
        )
        active = asyncio.create_task(agent.prompt_async("active"))
        await asyncio.sleep(0.005)
        queued = asyncio.create_task(agent.prompt_async("queued"))
        closing = asyncio.create_task(agent.aclose())
        assert (await active).output == "done"
        await closing
        try:
            await queued
        except RuntimeError as exc:
            assert "closing or closed" in str(exc)
        else:
            raise AssertionError("Expected closing to fence queued prompt")

    asyncio.run(scenario())


def test_aclose_finishes_cleanup_when_cancelled(tmp_path: Path) -> None:
    async def scenario() -> None:
        ConcurrentProvider.reset()
        agent = Agent(
            provider=ConcurrentProvider("done", delay=0.08),
            config=config(tmp_path),
        )
        prompt = asyncio.create_task(agent.prompt_async("active"))
        await asyncio.sleep(0.005)
        closing = asyncio.create_task(agent.aclose())
        while not agent._closing:
            await asyncio.sleep(0)
        closing.cancel()
        try:
            await closing
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("Expected close cancellation")

        assert agent.closed
        assert (await prompt).output == "done"
        assert ConcurrentProvider.closed == 1

    asyncio.run(scenario())


def test_close_is_idempotent_and_prompt_after_close_fails(
    tmp_path: Path,
) -> None:
    ConcurrentProvider.reset()
    agent = Agent(provider=ConcurrentProvider("done"), config=config(tmp_path))
    agent.close()
    agent.close()
    assert agent.closed
    assert ConcurrentProvider.closed == 1
    try:
        agent.prompt("hello")
    except RuntimeError as exc:
        assert "closed" in str(exc)
    else:
        raise AssertionError("Expected prompting a closed agent to fail")


def test_fork_does_not_duplicate_shared_provider_ownership(
    tmp_path: Path,
) -> None:
    ConcurrentProvider.reset()
    agent = Agent(provider=ConcurrentProvider("done"), config=config(tmp_path))
    forked = agent.fork()
    forked.close()
    assert ConcurrentProvider.closed == 0
    agent.close()
    assert ConcurrentProvider.closed == 1


def test_original_close_keeps_shared_provider_alive_for_fork(
    tmp_path: Path,
) -> None:
    ConcurrentProvider.reset()
    agent = Agent(provider=ConcurrentProvider("done"), config=config(tmp_path))
    forked = agent.fork()
    agent.close()
    assert ConcurrentProvider.closed == 0
    assert forked.prompt("hello").output == "done"
    forked.close()
    assert ConcurrentProvider.closed == 1


def test_prompt_callback_cannot_close_agent(tmp_path: Path) -> None:
    agent = Agent(provider=ConcurrentProvider("done"), config=config(tmp_path))

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
    async def scenario() -> None:
        ConcurrentProvider.reset()
        progress: list[BatchProgress] = []
        tasks = [BatchTask(id=str(index), prompt=str(index)) for index in range(5)]

        def factory(task: BatchTask) -> Agent:
            return Agent(
                provider=ConcurrentProvider(task.id, delay=0.01 * (5 - int(task.id))),
                config=config(tmp_path),
            )

        result = await run_many(
            tasks,
            agent_factory=factory,
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
        assert ConcurrentProvider.maximum_active == 2
        assert ConcurrentProvider.closed == 5
        assert result.completed_count == 5
        assert result.failed_count == 0
        assert result.usage.calls == 5
        assert result.usage.input_tokens == 10
        assert result.usage.output_tokens == 15
        assert result.usage.total_tokens == 30
        assert result.usage.cache_creation_input_tokens == 10
        assert [event.completed for event in progress] == [1, 2, 3, 4, 5]
        assert {event.task_id for event in progress} == {
            "0",
            "1",
            "2",
            "3",
            "4",
        }

    asyncio.run(scenario())


def test_run_many_retries_errors_and_closes_every_agent(tmp_path: Path) -> None:
    async def scenario() -> None:
        ConcurrentProvider.reset()
        attempts: dict[str, int] = {}

        def factory(task: BatchTask) -> Agent:
            attempt = attempts.get(task.id, 0) + 1
            attempts[task.id] = attempt
            return Agent(
                provider=ConcurrentProvider(
                    task.id,
                    fail=task.id == "always" or (task.id == "retry" and attempt == 1),
                ),
                config=config(tmp_path),
            )

        result = await run_many(
            [
                BatchTask(id="retry", prompt="retry"),
                BatchTask(id="always", prompt="always"),
            ],
            agent_factory=factory,
            max_attempts=2,
        )
        assert result.items[0].status == "completed"
        assert result.items[0].attempts == 2
        assert result.items[1].status == "error"
        assert isinstance(result.items[1].error, RuntimeError)
        assert result.failed_count == 1
        # Failed provider calls report no usage; the successful retry does.
        assert result.usage.calls == 1
        assert result.usage.total_tokens == 6
        assert ConcurrentProvider.closed == 4

    asyncio.run(scenario())


def test_run_many_accepts_async_factory_and_retry_policy(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        attempts = 0

        async def factory(task: BatchTask) -> Agent:
            nonlocal attempts
            del task
            await asyncio.sleep(0)
            attempts += 1
            return Agent(
                provider=ConcurrentProvider("done", fail=True),
                config=config(tmp_path),
            )

        result = await run_many(
            [BatchTask(id="one", prompt="one")],
            agent_factory=factory,
            max_attempts=3,
            should_retry=lambda exc: False,
        )
        assert attempts == 1
        assert result.items[0].attempts == 1

    asyncio.run(scenario())


def test_run_many_isolates_retry_predicate_errors(tmp_path: Path) -> None:
    async def scenario() -> None:
        ConcurrentProvider.reset()

        def retry(_error: Exception) -> bool:
            raise ValueError("retry policy failed")

        result = await run_many(
            [
                BatchTask(id="failed", prompt="failed"),
                BatchTask(id="completed", prompt="completed"),
            ],
            agent_factory=lambda task: Agent(
                provider=ConcurrentProvider(task.id, fail=task.id == "failed"),
                config=config(tmp_path),
            ),
            max_attempts=2,
            should_retry=retry,
        )

        assert result.items[0].status == "error"
        assert isinstance(result.items[0].error, ValueError)
        assert str(result.items[0].error) == "retry policy failed"
        assert result.items[1].status == "completed"
        assert ConcurrentProvider.closed == 2

    asyncio.run(scenario())


def test_run_many_runs_sync_factory_outside_event_loop(tmp_path: Path) -> None:
    async def scenario() -> None:
        loop_thread = threading.get_ident()
        release = threading.Event()
        factory_threads: list[int] = []
        asyncio.get_running_loop().call_later(0.01, release.set)

        def factory(task: BatchTask) -> Agent:
            factory_threads.append(threading.get_ident())
            if not release.wait(timeout=0.5):
                raise RuntimeError("event loop was blocked by agent_factory")
            return Agent(
                provider=ConcurrentProvider(task.id),
                config=config(tmp_path),
            )

        result = await run_many(
            [BatchTask(id="one", prompt="one")],
            agent_factory=factory,
        )

        assert result.items[0].status == "completed"
        assert factory_threads != [loop_thread]

    asyncio.run(scenario())


def test_run_many_isolates_invalid_factory_result(tmp_path: Path) -> None:
    async def scenario() -> None:
        def invalid_factory(task: BatchTask) -> Agent:
            return cast(Agent, task)

        result = await run_many(
            [BatchTask(id="one", prompt="one")],
            agent_factory=invalid_factory,
        )
        assert result.items[0].status == "error"
        assert isinstance(result.items[0].error, TypeError)
        assert "return an Agent" in str(result.items[0].error)

    del tmp_path
    asyncio.run(scenario())


def test_run_many_isolates_progress_callback_errors(tmp_path: Path) -> None:
    async def scenario() -> None:
        ConcurrentProvider.reset()

        def broken_progress(progress: BatchProgress) -> None:
            raise RuntimeError(f"progress failed for {progress.task_id}")

        result = await run_many(
            [
                BatchTask(id="one", prompt="one"),
                BatchTask(id="two", prompt="two"),
            ],
            agent_factory=lambda task: Agent(
                provider=ConcurrentProvider(task.id), config=config(tmp_path)
            ),
            on_progress=broken_progress,
        )
        assert result.completed_count == 2
        assert len(result.progress_errors) == 2
        assert ConcurrentProvider.closed == 2

    asyncio.run(scenario())


def test_run_many_excludes_historical_usage(tmp_path: Path) -> None:
    async def scenario() -> None:
        historical = Message.assistant("old").model_copy(
            update={"usage": TokenUsage(input_tokens=10, total_tokens=10)}
        )
        result = await run_many(
            [BatchTask(id="one", prompt="one")],
            agent_factory=lambda task: Agent(
                provider=ConcurrentProvider(task.id),
                config=RunConfig(
                    root=tmp_path,
                    tools=[],
                    include_agents_file=False,
                    messages=[historical],
                ),
            ),
        )
        assert result.usage.calls == 1
        assert result.usage.input_tokens == 2
        assert result.usage.total_tokens == 6

    asyncio.run(scenario())


def test_run_many_validates_unique_ids(tmp_path: Path) -> None:
    async def scenario() -> None:
        task = BatchTask(id="same", prompt="hello")
        try:
            await run_many(
                [task, task],
                agent_factory=lambda item: Agent(
                    provider=ConcurrentProvider(item.id),
                    config=config(tmp_path),
                ),
            )
        except ValueError as exc:
            assert "unique" in str(exc)
        else:
            raise AssertionError("Expected duplicate-id validation")

    asyncio.run(scenario())


def test_run_many_marks_timeouts_and_closes_agents(tmp_path: Path) -> None:
    async def scenario() -> None:
        CancellableProvider.stopped.clear()
        result = await run_many(
            [BatchTask(id="slow", prompt="wait")],
            agent_factory=lambda task: Agent(
                provider=CancellableProvider(), config=config(tmp_path)
            ),
            timeout=0.01,
        )
        assert result.items[0].status == "timed_out"
        assert isinstance(result.items[0].error, TimeoutError)
        assert CancellableProvider.stopped.is_set()
        assert CancellableProvider.closed.is_set()

    asyncio.run(scenario())


def test_run_many_cancellation_stops_and_closes_active_agent(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        CancellableProvider.stopped.clear()
        CancellableProvider.closed.clear()
        batch = asyncio.create_task(
            run_many(
                [BatchTask(id="slow", prompt="wait")],
                agent_factory=lambda task: Agent(
                    provider=CancellableProvider(), config=config(tmp_path)
                ),
            )
        )
        await asyncio.sleep(0.01)
        batch.cancel()
        try:
            await batch
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("Expected batch cancellation")
        assert CancellableProvider.stopped.is_set()
        assert CancellableProvider.closed.is_set()

    asyncio.run(scenario())


def test_factory_cleanup_failure_does_not_mask_cancellation(
    tmp_path: Path,
) -> None:
    from yoke.ai.sdk.batch import _call_factory

    class CleanupFailingAgent(Agent):
        async def aclose(self) -> None:
            await super().aclose()
            raise RuntimeError("cleanup failed")

    async def scenario() -> None:
        def factory(_task: BatchTask) -> Agent:
            time.sleep(0.03)
            return CleanupFailingAgent(
                provider=ConcurrentProvider("done"), config=config(tmp_path)
            )

        call = asyncio.create_task(
            _call_factory(factory, BatchTask(id="cancel", prompt="cancel"))
        )
        await asyncio.sleep(0.005)
        call.cancel()
        try:
            await call
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("Expected factory cancellation")

    asyncio.run(scenario())


def test_run_many_rejects_reused_agent(tmp_path: Path) -> None:
    async def scenario() -> None:
        ConcurrentProvider.reset()
        shared = Agent(provider=ConcurrentProvider("done"), config=config(tmp_path))
        result = await run_many(
            [
                BatchTask(id="one", prompt="one"),
                BatchTask(id="two", prompt="two"),
            ],
            agent_factory=lambda task: shared,
            max_concurrency=1,
        )
        assert result.items[0].status == "completed"
        assert result.items[1].status == "error"
        assert isinstance(result.items[1].error, ValueError)
        assert "fresh Agent" in str(result.items[1].error)
        assert result.usage.calls == 1
        assert ConcurrentProvider.closed == 1

    asyncio.run(scenario())


def test_prompt_async_rejects_a_second_event_loop(tmp_path: Path) -> None:
    agent = Agent(provider=ConcurrentProvider("done"), config=config(tmp_path))

    try:
        assert asyncio.run(agent.prompt_async("first")).output == "done"
        try:
            asyncio.run(agent.prompt_async("second"))
        except RuntimeError as exc:
            assert "multiple event loops" in str(exc)
        else:
            raise AssertionError("Expected cross-loop use to fail")
    finally:
        agent.close()


def test_prompt_async_cross_thread_loops_do_not_hang(tmp_path: Path) -> None:
    agent = Agent(
        provider=ConcurrentProvider("done", delay=0.03),
        config=config(tmp_path),
    )
    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def run_prompt() -> None:
        barrier.wait()
        try:
            asyncio.run(agent.prompt_async("run"))
        except RuntimeError as exc:
            outcomes.append(str(exc))
        else:
            outcomes.append("completed")

    threads = [threading.Thread(target=run_prompt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=1)

    try:
        assert all(not thread.is_alive() for thread in threads)
        assert outcomes.count("completed") == 1
        assert sum("multiple event loops" in item for item in outcomes) == 1
    finally:
        agent.close()


def test_run_many_duplicate_agent_does_not_close_active_owner(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        ConcurrentProvider.reset()
        shared = Agent(
            provider=ConcurrentProvider("done", delay=0.05),
            config=config(tmp_path),
        )
        result = await run_many(
            [
                BatchTask(id="one", prompt="one"),
                BatchTask(id="two", prompt="two"),
            ],
            agent_factory=lambda task: shared,
            max_concurrency=2,
        )
        assert sorted(item.status for item in result.items) == [
            "completed",
            "error",
        ]
        assert ConcurrentProvider.closed == 1

    asyncio.run(scenario())


def test_run_many_rejects_shared_provider_identity(tmp_path: Path) -> None:
    async def scenario() -> None:
        ConcurrentProvider.reset()
        provider = ConcurrentProvider("done", delay=0.05)
        result = await run_many(
            [
                BatchTask(id="one", prompt="one"),
                BatchTask(id="two", prompt="two"),
            ],
            agent_factory=lambda task: Agent(
                provider=provider, config=config(tmp_path)
            ),
            max_concurrency=2,
        )
        assert sorted(item.status for item in result.items) == [
            "completed",
            "error",
        ]
        errors = [item.error for item in result.items if item.error is not None]
        assert len(errors) == 1
        assert "fresh Provider" in str(errors[0])
        assert ConcurrentProvider.closed == 1

    asyncio.run(scenario())
