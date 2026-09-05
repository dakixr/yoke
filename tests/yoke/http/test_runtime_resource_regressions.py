from __future__ import annotations

# ruff: noqa: D100,D101,D102,D103,S101,SLF001

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from threading import Lock
import time

import pytest

from yoke.agent.loop import RuntimeAgent
from yoke.agent.loop import in_process_tool as in_process_tool_module
from yoke.agent.loop.in_process_tool import execute_in_process_tool
from yoke.agent.models import Message
from yoke.agent.tools import LocalTool
from yoke.http.services.session_runtime.reaper import retire_resource
from yoke.http.services.session_runtime.resources import SessionRuntimeResources
from yoke.session import SessionRecord


class TrackingProvider:
    supports_image_inputs = True
    max_images_per_message = 50

    def __init__(self, name: str = "provider") -> None:
        self.name = name
        self.close_count = 0
        self._lock = Lock()

    def complete(self, messages, tools) -> Message:  # noqa: ANN001
        del messages, tools
        return Message.assistant("done")

    def close(self) -> None:
        with self._lock:
            self.close_count += 1


def _resources(agent: RuntimeAgent, *, workers: int = 1):
    executor = ThreadPoolExecutor(max_workers=workers)
    resources = SessionRuntimeResources(
        session_id="session-a",
        agent_factory=lambda _record: agent,
        executor=executor,
        on_process_change=lambda: None,
    )
    resources.ensure_primary(SessionRecord(id="session-a"), load_state=False)
    return resources, executor


def test_idle_primary_cleanup_bypasses_a_saturated_runtime_executor() -> None:
    provider = TrackingProvider()
    resources, executor = _resources(RuntimeAgent(provider, []))
    occupied = Event()
    release = Event()

    def block_worker() -> None:
        occupied.set()
        release.wait(2)

    blocker = executor.submit(block_worker)
    try:
        assert occupied.wait(1)
        asyncio.run(asyncio.wait_for(resources.close(), timeout=1))
        assert provider.close_count == 1
    finally:
        release.set()
        blocker.result(timeout=1)
        executor.shutdown(wait=True)


def test_concurrent_close_callers_join_one_physical_close() -> None:
    provider = TrackingProvider()
    close_started = Event()
    release_close = Event()

    class BlockingCloseAgent(RuntimeAgent):
        close_count = 0

        def close(self) -> None:
            self.close_count += 1
            close_started.set()
            release_close.wait(2)
            super().close()

    agent = BlockingCloseAgent(provider, [])
    resources, executor = _resources(agent)

    async def scenario() -> None:
        first = asyncio.create_task(resources.close())
        assert await asyncio.to_thread(close_started.wait, 1)
        second = asyncio.create_task(resources.close())
        await asyncio.sleep(0)
        assert not first.done()
        assert not second.done()
        release_close.set()
        await asyncio.gather(first, second)

    try:
        asyncio.run(scenario())
    finally:
        release_close.set()
        executor.shutdown(wait=True)
    assert agent.close_count == 1
    assert provider.close_count == 1


def test_concurrent_reap_is_single_flight() -> None:
    primary_provider = TrackingProvider("primary")
    fork_provider = TrackingProvider("fork")

    class ForkingProvider(TrackingProvider):
        def fork_for_turn(self) -> TrackingProvider:
            return fork_provider

    primary_provider = ForkingProvider("primary")
    resources, executor = _resources(RuntimeAgent(primary_provider, []))
    fork = resources.prepare_turn(
        SessionRecord(id="session-a"),
        active_entries=[],
        load_active_entries=list,
    )
    assert isinstance(fork, RuntimeAgent)
    close_count = 0
    original_close = fork.close

    def counted_close() -> None:
        nonlocal close_count
        close_count += 1
        original_close()

    fork.close = counted_close  # type: ignore[method-assign]

    async def reap_twice() -> None:
        await asyncio.gather(resources.reap(fork), resources.reap(fork))

    try:
        asyncio.run(reap_twice())
    finally:
        asyncio.run(resources.close())
        executor.shutdown(wait=True)
    assert close_count == 1
    assert fork_provider.close_count == 1


def test_provider_shared_by_retired_fork_survives_promotion_swap() -> None:
    distinct = TrackingProvider("distinct")

    class SequencedProvider(TrackingProvider):
        def __init__(self) -> None:
            super().__init__("shared")
            self.forks = 0

        def fork_for_turn(self) -> TrackingProvider:
            self.forks += 1
            return self if self.forks == 1 else distinct

    shared = SequencedProvider()
    resources, executor = _resources(RuntimeAgent(shared, []))
    first = resources.prepare_turn(
        SessionRecord(id="session-a"),
        active_entries=[],
        load_active_entries=list,
    )
    second = resources.prepare_turn(
        SessionRecord(id="session-a"),
        active_entries=[],
        load_active_entries=list,
    )
    assert isinstance(first, RuntimeAgent)
    assert isinstance(second, RuntimeAgent)
    resources.promote(second)
    try:
        asyncio.run(resources.reap(second))
        assert shared.close_count == 0
        asyncio.run(resources.reap(first))
        assert shared.close_count == 1
        assert distinct.close_count == 0
    finally:
        asyncio.run(resources.close())
        executor.shutdown(wait=True)
    assert distinct.close_count == 1


def test_failed_deferred_active_entry_load_reaps_the_registered_fork() -> None:
    fork_provider = TrackingProvider("failed-fork")

    class ForkingProvider(TrackingProvider):
        def fork_for_turn(self) -> TrackingProvider:
            return fork_provider

    resources, executor = _resources(RuntimeAgent(ForkingProvider("primary"), []))

    def fail_load() -> list:
        raise OSError("deferred active path failed")

    try:
        with pytest.raises(OSError, match="deferred active path failed"):
            resources.prepare_turn(
                SessionRecord(id="session-a"),
                active_entries=None,
                load_active_entries=fail_load,
            )
        deadline = time.monotonic() + 1
        while fork_provider.close_count == 0 and time.monotonic() < deadline:
            time.sleep(0.005)
        assert fork_provider.close_count == 1
    finally:
        asyncio.run(resources.close())
        executor.shutdown(wait=True)


def test_nonterminal_in_process_fork_close_retains_every_owner_until_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = Event()
    release = Event()
    finished = Event()
    fork_provider = TrackingProvider("fork")

    class BlockingTool(LocalTool):
        name = "blocking"
        description = "Block until the test releases this tool."
        execute_in_process = True

        def execute(self) -> dict[str, object]:
            started.set()
            try:
                release.wait(2)
                return {"ok": True}
            finally:
                finished.set()

    class ForkingProvider(TrackingProvider):
        def fork_for_turn(self) -> TrackingProvider:
            return fork_provider

    primary = RuntimeAgent(
        ForkingProvider("primary"),
        [BlockingTool.bind()],
        tool_root=tmp_path,
    )
    resources, executor = _resources(primary)
    fork = resources.prepare_turn(
        SessionRecord(id="session-a"),
        active_entries=[],
        load_active_entries=list,
    )
    assert isinstance(fork, RuntimeAgent)
    _, stopped = execute_in_process_tool(
        tools=fork.tools,
        name="blocking",
        arguments={},
        stop_requested=lambda: True,
    )
    assert stopped is True
    assert started.wait(1)
    monkeypatch.setattr(
        in_process_tool_module,
        "IN_PROCESS_TOOL_SHUTDOWN_SECONDS",
        0.01,
    )

    async def scenario() -> None:
        reap = asyncio.create_task(resources.reap(fork))
        try:
            await asyncio.sleep(0.05)
            assert not reap.done()
            assert fork.closed is False
            assert resources._turn_agents[id(fork)] is fork
            assert fork_provider.close_count == 0
        finally:
            release.set()
        assert await asyncio.to_thread(finished.wait, 1)
        await asyncio.wait_for(reap, timeout=1)

    try:
        asyncio.run(scenario())
    finally:
        release.set()
        asyncio.run(resources.close())
        executor.shutdown(wait=True)
    assert fork.closed is True
    assert fork_provider.close_count == 1


def test_blocked_retirement_does_not_delay_other_sessions() -> None:
    from threading import Event

    started, release = Event(), Event()
    attempts = 0

    def blocked() -> bool:
        started.set()
        assert release.wait(timeout=5)
        return True

    def retry() -> bool:
        nonlocal attempts
        attempts += 1
        return attempts == 2

    first = retire_resource(blocked)
    try:
        assert started.wait(timeout=1)
        second = retire_resource(retry)
        second.result(timeout=2)
        assert attempts == 2
        assert not first.done()
    finally:
        release.set()
        first.result(timeout=2)
