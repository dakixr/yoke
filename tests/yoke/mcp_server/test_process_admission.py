"""Process control remains usable when execution admission is saturated."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import threading

from anyio import CancelScope
from anyio.to_thread import current_default_thread_limiter, run_sync
import pytest

from yoke.mcp_server.config import MCPServerConfig
from yoke.mcp_server.server import create_service

from .helpers import memory_client, structured


@pytest.mark.parametrize("control", ["process_input", "process_cancel"])
def test_control_can_finish_initial_wait_with_all_execution_slots_occupied(
    tmp_path: Path, control: str
) -> None:
    async def scenario() -> None:
        service = create_service(
            MCPServerConfig(
                root=tmp_path, max_concurrent_calls=1, max_concurrent_process_starts=1
            )
        )
        async with memory_client(service) as client:
            execution = service.adapter.execution
            active = asyncio.create_task(
                service.adapter.call_tool(
                    "command_exec",
                    {
                        "argv": [
                            sys.executable,
                            "-u",
                            "-c",
                            "print('ready', flush=True); print('echo:' + input())",
                        ],
                    },
                )
            )
            async with asyncio.timeout(5):
                while not service.runtime.manager.snapshots():
                    await asyncio.sleep(0.001)
            session = service.runtime.manager.snapshots()[0].session_id
            queued_cancel = threading.Event()
            queued = asyncio.create_task(
                execution.dispatch(
                    "command_exec",
                    {"argv": [sys.executable, "-c", "open('unexpected', 'w').close()"]},
                    cancel=queued_cancel,
                )
            )
            try:
                await asyncio.sleep(0.05)
                assert service.runtime._total.locked()
                assert service.runtime._process_starts.locked()
                assert not active.done() and not queued.done()
                async with asyncio.timeout(5):
                    ready = structured(
                        await client.call_tool(
                            "process_read",
                            {
                                "sessions": [{"session_id": session}],
                                "until": "output_or_completion",
                                "wait_ms": 5000,
                            },
                        )
                    )
                    assert ready["items"][0]["output"] == "ready\n"
                    # Cancelled queued work must not start when control frees
                    # the active initial observation's execution slot.
                    queued_cancel.set()
                    result = structured(
                        await client.call_tool(
                            control,
                            {
                                "session_id": session,
                                **(
                                    {"chars": "answer\n", "wait_ms": 5000}
                                    if control == "process_input"
                                    else {}
                                ),
                            },
                        )
                    )
                    assert result["ok"]
                    assert result["running"] is False
                    if control == "process_input":
                        assert result["output"] == "ready\necho:answer\n"
                    initial = structured(await active)
                    assert initial["running"] is False
                    assert (await queued)["reason"] == "cancelled"
                assert not (tmp_path / "unexpected").exists()
            finally:
                queued_cancel.set()
                for task in (active, queued):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(active, queued, return_exceptions=True)

    asyncio.run(scenario())


def test_queued_start_does_not_consume_general_operation_slot(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = create_service(
            MCPServerConfig(
                root=tmp_path, max_concurrent_calls=1, max_concurrent_process_starts=1
            )
        )
        (tmp_path / "readable.txt").write_text("available\n")
        async with memory_client(service):
            execution = service.adapter.execution
            await service.runtime._process_starts.acquire()
            cancel = threading.Event()
            task = asyncio.create_task(
                execution.dispatch(
                    "command_exec",
                    {"argv": [sys.executable, "-c", "open('unexpected', 'w').close()"]},
                    cancel=cancel,
                )
            )
            try:
                await asyncio.sleep(0.05)
                assert not task.done()
                async with asyncio.timeout(2):
                    result = await execution.dispatch(
                        "read_file", {"path": "readable.txt"}
                    )
                    assert result["ok"]
                    cancel.set()
                    assert (await task)["reason"] == "cancelled"
                assert not service.runtime.manager.snapshots()
            finally:
                cancel.set()
                service.runtime._process_starts.release()
                await asyncio.gather(task, return_exceptions=True)
            assert not (tmp_path / "unexpected").exists()

    asyncio.run(scenario())


def test_input_has_worker_capacity_separate_from_normal_calls(tmp_path: Path) -> None:
    async def scenario() -> None:
        # A single default executor worker is occupied by initial observation.
        asyncio.get_running_loop().set_default_executor(
            ThreadPoolExecutor(max_workers=1)
        )
        limiter = current_default_thread_limiter()
        previous_tokens = limiter.total_tokens
        limiter.total_tokens = 1
        entered, release = threading.Event(), threading.Event()
        service = create_service(
            MCPServerConfig(
                root=tmp_path, max_concurrent_calls=2, max_concurrent_process_starts=1
            )
        )
        async with memory_client(service):
            active = asyncio.create_task(
                service.adapter.call_tool(
                    "command_exec",
                    {
                        "argv": [sys.executable, "-u", "-c", "print(input())"],
                    },
                )
            )

            def blocking_operation() -> None:
                entered.set()
                assert release.wait(10), "Test did not release the normal operation"

            async def normal_call() -> None:
                async with service.runtime._total:
                    await run_sync(blocking_operation)

            normal = asyncio.create_task(normal_call())
            try:
                async with asyncio.timeout(5):
                    while (
                        not entered.is_set() or not service.runtime.manager.snapshots()
                    ):
                        await asyncio.sleep(0.001)
                assert service.runtime._total.locked()
                assert service.runtime._process_starts.locked()
                session = service.runtime.manager.snapshots()[0].session_id
                async with asyncio.timeout(5):
                    observed = await service.adapter.execution.dispatch(
                        "process_read",
                        {"sessions": [{"session_id": session}], "wait_ms": 0},
                    )
                    assert observed["reason"] == "snapshot"
                    assert observed["items"][0]["running"]
                    result = structured(
                        await service.adapter.call_tool(
                            "process_input",
                            {"session_id": session, "chars": "kept\n", "wait_ms": 5000},
                        )
                    )
                    assert result["output"] == "kept\n"
                    assert structured(await active)["exit_code"] == 0
            finally:
                release.set()
                limiter.total_tokens = previous_tokens
                if not active.done():
                    active.cancel()
                await asyncio.gather(active, normal, return_exceptions=True)

    asyncio.run(scenario())


def test_anyio_cancelled_input_waiting_for_writer_joins_without_writing(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with memory_client(service) as client:
            running = structured(
                await client.call_tool(
                    "python_exec", {"code": "print(input())", "mode": "background"}
                )
            )
            managed = service.runtime.manager._get(running["session_id"])
            managed.input_lock.acquire()
            scopes: list[CancelScope] = []

            async def call() -> None:
                with CancelScope() as scope:
                    scopes.append(scope)
                    await service.adapter.call_tool(
                        "process_input",
                        {"session_id": running["session_id"], "chars": "discard\n"},
                    )
                    pytest.fail("Input acquired a held writer lock")

            task = asyncio.create_task(call())
            try:
                await asyncio.sleep(0.05)
                scopes[0].cancel()
                async with asyncio.timeout(2):
                    await task
                assert scopes[0].cancelled_caught
                assert not service.runtime._controls.locked()
            finally:
                managed.input_lock.release()
            result = structured(
                await client.call_tool(
                    "process_input",
                    {
                        "session_id": running["session_id"],
                        "chars": "kept\n",
                        "wait_ms": 5000,
                    },
                )
            )
            assert result["output"] == "kept\n"

    asyncio.run(scenario())
