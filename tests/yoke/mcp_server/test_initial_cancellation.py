"""Cancellation of real initial process calls, including transport cancel scopes."""

from __future__ import annotations

import asyncio
from pathlib import Path
import sys
import threading

from anyio import CancelScope
import pytest

from yoke.agent.tools.processes import base
from yoke.agent.tools.python_exec import PythonExecTool
from yoke.mcp_server.config import MCPServerConfig
from yoke.mcp_server.server import create_service

from .helpers import memory_client, structured


async def wait_event(event: threading.Event) -> None:
    async with asyncio.timeout(5):
        while not event.is_set():
            await asyncio.sleep(0.001)


@pytest.mark.parametrize("name", ["command_exec", "python_exec"])
@pytest.mark.parametrize("cancellation", ["asyncio", "anyio"])
def test_initial_transport_cancellation_joins_worker_and_keeps_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, cancellation: str
) -> None:
    entered, settled = threading.Event(), threading.Event()
    original = base.read_processes

    def observe(*args, **kwargs):
        entered.set()
        try:
            return original(*args, **kwargs)
        finally:
            settled.set()

    monkeypatch.setattr(base, "read_processes", observe)

    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with memory_client(service):
            scopes: list[CancelScope] = []
            code = "print('ready', flush=True); input(); print('kept', flush=True)"
            if name == "python_exec":
                code += "; from yoke_mcp import output; output.emit('bridge-alive')"
            arguments: dict[str, object] = (
                {"code": code}
                if name == "python_exec"
                else {"argv": [sys.executable, "-u", "-c", code]}
            )

            async def call() -> None:
                with CancelScope() as scope:
                    scopes.append(scope)
                    await service.adapter.call_tool(name, arguments)
                    pytest.fail("Initial observation completed before cancellation")

            task = asyncio.create_task(call())
            await wait_event(entered)
            session = service.runtime.manager.snapshots()[0].session_id
            if cancellation == "asyncio":
                task.cancel()
            else:
                scopes[0].cancel()
            async with asyncio.timeout(2):
                if cancellation == "asyncio":
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    await task
                    assert scopes[0].cancelled_caught
            assert settled.is_set()
            assert service.runtime.manager.snapshot(session).status == "running"
            execution = service.adapter.execution
            if name == "python_exec":
                token = execution._sessions[session]
                assert not execution.bridge._runs[token].cancelled.is_set()
            async with asyncio.timeout(5):
                result = structured(
                    await service.adapter.call_tool(
                        "process_input",
                        {"session_id": session, "chars": "go\n", "wait_ms": 5000},
                    )
                )
            assert result["exit_code"] == 0
            assert "kept\n" in result["output"]
            if name == "python_exec":
                assert "bridge-alive" in result["output"]
                async with asyncio.timeout(2):
                    while execution._sessions or execution._watchers:
                        await asyncio.sleep(0.01)
                assert not execution.bridge._runs

    asyncio.run(scenario())


def test_python_dispatch_cancel_event_stops_only_observation(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with memory_client(service):
            execution = service.adapter.execution
            cancel = threading.Event()
            task = asyncio.create_task(
                execution.dispatch(
                    "python_exec",
                    {"code": "input()"},
                    cancel=cancel,
                )
            )
            async with asyncio.timeout(5):
                while not service.runtime.manager.snapshots():
                    await asyncio.sleep(0.001)
            cancel.set()
            async with asyncio.timeout(2):
                result = await task
            assert result["reason"] == "cancelled"
            assert result["running"]
            session = result["session_id"]
            assert isinstance(result["cursor"], str)
            assert result["cursor"].startswith("pc1_")
            assert service.runtime.manager.snapshot(session).status == "running"
            token = execution._sessions[session]
            assert not execution.bridge._runs[token].cancelled.is_set()
            await execution.dispatch("process_cancel", {"session_id": session})
            assert not execution.bridge._runs

    asyncio.run(scenario())


def test_python_cancel_event_prevents_queued_orchestration_from_spawning(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with memory_client(service):
            execution = service.adapter.execution
            execution._orchestrations = asyncio.Semaphore(1)
            await execution._orchestrations.acquire()
            cancel = threading.Event()
            try:
                task = asyncio.create_task(
                    execution.dispatch(
                        "python_exec",
                        {"code": "open('unexpected', 'w').close()"},
                        cancel=cancel,
                    )
                )
                await asyncio.sleep(0.05)
                assert not task.done()
                cancel.set()
                async with asyncio.timeout(2):
                    result = await task
                assert result["reason"] == "cancelled"
                assert not service.runtime.manager.snapshots()
                assert not execution.bridge._runs
            finally:
                execution._orchestrations.release()
            await asyncio.sleep(0.05)
            assert not (tmp_path / "unexpected").exists()
            assert not service.runtime.manager.snapshots()

    asyncio.run(scenario())


@pytest.mark.parametrize("cancellation", ["asyncio", "anyio"])
def test_python_transport_cancellation_during_settlement_adopts_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancellation: str
) -> None:
    settling, release = threading.Event(), threading.Event()
    execute = PythonExecTool.execute

    def delayed(self):
        result = execute(self)
        settling.set()
        assert release.wait(5), "Test did not release the settling worker"
        return result

    monkeypatch.setattr(PythonExecTool, "execute", delayed)

    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with memory_client(service):
            execution = service.adapter.execution
            cancel = threading.Event()
            scopes: list[CancelScope] = []

            async def call() -> None:
                with CancelScope() as scope:
                    scopes.append(scope)
                    await execution.dispatch(
                        "python_exec",
                        {"code": "input()"},
                        cancel=cancel,
                    )

            task = asyncio.create_task(call())
            try:
                async with asyncio.timeout(5):
                    while not service.runtime.manager.snapshots():
                        await asyncio.sleep(0.001)
                cancel.set()
                await wait_event(settling)
                if cancellation == "asyncio":
                    task.cancel()
                    await asyncio.sleep(0.01)
                    task.cancel()
                else:
                    scopes[0].cancel()
                await asyncio.sleep(0.05)
                assert not task.done()
            finally:
                release.set()
            async with asyncio.timeout(2):
                if cancellation == "asyncio":
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    await task
                    assert scopes[0].cancelled_caught
            session = service.runtime.manager.snapshots()[0].session_id
            token = execution._sessions[session]
            assert token in execution.bridge._runs
            assert execution._watchers
            await execution.dispatch("process_cancel", {"session_id": session})
            async with asyncio.timeout(2):
                while execution._watchers:
                    await asyncio.sleep(0.01)
            assert not execution.bridge._runs
            assert not execution._sessions

    asyncio.run(scenario())
