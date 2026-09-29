"""Real SDK subprocesses across managed command, Python, and MCP entry points."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import shlex
import sys
import threading
from typing import Any, cast

import pytest

from yoke.agent.tools.command import ExecCommandTool
from yoke.agent.tools.command_process_manager import CommandProcessManager
from yoke.agent.tools.python_exec import PythonExecTool
from yoke.ai.providers.usage_context import usage_metric_context
from yoke.mcp_server.config import MCPServerConfig
from yoke.mcp_server.server import create_service

from tests.yoke.mcp_server.helpers import memory_client, structured


WORKER = Path(__file__).with_name("agent_tracking_worker.py")
Rows = list[dict[str, object]]


class RegistryReceipt:
    """Wait for observed registry transitions, rather than polling or sleeping."""

    def __init__(self, manager: CommandProcessManager) -> None:
        self.manager = manager
        self.condition = threading.Condition()
        self.unsubscribe = manager.agent_runs.subscribe(self.changed)

    def changed(self) -> None:
        with self.condition:
            self.condition.notify_all()

    def wait(self, predicate: Callable[[Rows], bool]) -> Rows:
        with self.condition:
            ready = self.condition.wait_for(
                lambda: predicate(self.manager.agent_runs.snapshots()), timeout=20
            )
        rows = self.manager.agent_runs.snapshots()
        assert ready, {"runs": rows, "processes": self.manager.snapshots()}
        return rows


def wait_for_output(
    manager: CommandProcessManager, session_id: int, marker: str
) -> None:
    """A process-output notification is the fixture's explicit control receipt."""
    condition = threading.Condition()

    def changed() -> None:
        with condition:
            condition.notify_all()

    unsubscribe = manager.subscribe(changed)
    try:
        with condition:
            assert condition.wait_for(
                lambda: marker in manager.snapshot(session_id).output_tail,
                timeout=20,
            ), manager.snapshot(session_id)
    finally:
        unsubscribe()


def start(
    manager: CommandProcessManager, root: Path, *, mode: str, worker_mode: str = "mixed"
) -> dict[str, object]:
    argv = [sys.executable, str(WORKER), worker_mode]
    arguments: dict[str, object]
    if mode == "python":
        tool = PythonExecTool.bind(root=root, command_process_manager=manager)
        arguments = {
            "mode": "background",
            "timeout": 45,
            "code": (
                f"import runpy,sys; sys.argv={argv[1:]!r}; "
                f"runpy.run_path({str(WORKER)!r}, run_name='__main__')"
            ),
        }
    else:
        tool = ExecCommandTool.bind(root=root, command_process_manager=manager)
        arguments = {"mode": "background"}
        arguments.update(
            {"argv": argv}
            if mode == "argv"
            else {"cmd": shlex.join(argv), "login": False}
        )
    result = tool.parse_arguments(arguments).execute()
    assert result["ok"], result
    assert type(result["session_id"]) is int, result
    return result


def mixed_is_ready(rows: Rows) -> bool:
    return {str(row["name"]): row["status"] for row in rows} == {
        "success": "completed",
        "failed": "failed",
        "waiting": "running",
    }


@pytest.mark.parametrize("mode", ["python", "argv", "shell"])
def test_independent_runs_in_one_process_and_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    monkeypatch.setenv("YOKE_USAGE_METRIC_LOG_DIR", str(tmp_path / "usage"))
    manager = CommandProcessManager()
    receipt = RegistryReceipt(manager)
    try:
        with usage_metric_context(surface="http", session_id="native-owner"):
            launched = start(manager, tmp_path, mode=mode)
        rows = receipt.wait(mixed_is_ready)
        assert len({row["agentId"] for row in rows}) == 3
        assert len({row["runId"] for row in rows}) == 3
        assert {row["sessionID"] for row in rows} == {"native-owner"}
        assert {row["runtimeSessionID"] for row in rows} == {launched["session_id"]}
        assert all(row["observation"] == "live" for row in rows)
        assert all(
            cast(dict[str, object], row["typedUsage"])["totalTokens"] == 9
            for row in rows
            if row["status"] == "completed"
        )
        assert "SECRET_" not in json.dumps(rows)
        manager.write_input(cast(int, launched["session_id"]), "release\n")
        final = receipt.wait(
            lambda items: (
                len(items) == 4 and all(row["status"] != "running" for row in items)
            )
        )
        reused = [row for row in final if row["name"] == "success"]
        assert len(reused) == 2
        assert len({row["agentId"] for row in reused}) == 1
        assert len({row["runId"] for row in reused}) == 2
        assert all(
            cast(dict[str, object], row["typedUsage"])["totalTokens"] == 9
            for row in final
            if row["status"] == "completed"
        )
        assert "SECRET_" not in json.dumps(final)
        usage = [
            json.loads(line)
            for path in (tmp_path / "usage").rglob("*.jsonl")
            for line in path.read_text().splitlines()
        ]
        assert {row["sdk_run_id"] for row in usage} <= {row["runId"] for row in final}
        assert len(usage) == 3
    finally:
        receipt.unsubscribe()
        manager.close()


def test_concurrent_hosts_cannot_cross_attribute_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("YOKE_USAGE_METRIC_LOG_DIR", str(tmp_path / "usage"))

    def run(owner: str) -> Rows:
        manager = CommandProcessManager()
        receipt = RegistryReceipt(manager)
        try:
            with usage_metric_context(surface="cli", session_id=owner):
                launched = start(manager, tmp_path, mode="python")
            rows = receipt.wait(mixed_is_ready)
            assert {row["sessionID"] for row in rows} == {owner}
            manager.write_input(cast(int, launched["session_id"]), "release\n")
            return receipt.wait(
                lambda items: (
                    len(items) == 4 and all(r["status"] != "running" for r in items)
                )
            )
        finally:
            receipt.unsubscribe()
            manager.close()

    with ThreadPoolExecutor(max_workers=2) as workers:
        one, two = list(workers.map(run, ["owner-one", "owner-two"]))
    assert {row["agentId"] for row in one}.isdisjoint(row["agentId"] for row in two)


def test_nested_sdk_tool_reports_to_original_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("YOKE_USAGE_METRIC_LOG_DIR", str(tmp_path / "usage"))
    manager = CommandProcessManager()
    receipt = RegistryReceipt(manager)
    try:
        with usage_metric_context(surface="cli", session_id="root-owner"):
            start(manager, tmp_path, mode="argv", worker_mode="nested")
        rows = receipt.wait(
            lambda items: (
                len(items) == 2 and all(row["status"] != "running" for row in items)
            )
        )
        assert {row["status"] for row in rows} == {"completed"}
        parent = next(row for row in rows if row["name"] == "nested-parent")
        child = next(row for row in rows if row["name"] == "nested-child")
        assert child["parentRunId"] == parent["runId"]
        assert child["parentAgentId"] == parent["agentId"]
        assert {row["sessionID"] for row in rows} == {"root-owner"}
        assert parent["lastToolName"] == "python_exec"
    finally:
        receipt.unsubscribe()
        manager.close()


def test_worker_crash_does_not_claim_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("YOKE_USAGE_METRIC_LOG_DIR", str(tmp_path / "usage"))
    manager = CommandProcessManager()
    receipt = RegistryReceipt(manager)
    try:
        launched = start(manager, tmp_path, mode="argv")
        receipt.wait(mixed_is_ready)
        manager.write_input(cast(int, launched["session_id"]), "crash\n")
        rows = receipt.wait(
            lambda items: any(
                row["name"] == "waiting" and row["status"] == "interrupted"
                for row in items
            )
        )
        assert (
            next(row for row in rows if row["name"] == "success")["status"]
            == "completed"
        )
        assert (
            next(row for row in rows if row["name"] == "failed")["status"] == "failed"
        )
    finally:
        receipt.unsubscribe()
        manager.close()


def test_async_cancel_stays_running_until_real_worker_drains(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("YOKE_USAGE_METRIC_LOG_DIR", str(tmp_path / "usage"))
    manager = CommandProcessManager()
    receipt = RegistryReceipt(manager)
    try:
        launched = start(manager, tmp_path, mode="argv", worker_mode="cancellation")
        session_id = cast(int, launched["session_id"])
        wait_for_output(manager, session_id, "PROVIDER_ENTERED:cancelled")
        manager.write_input(session_id, "cancel\n")
        wait_for_output(manager, session_id, "CANCEL_REQUESTED")
        rows = manager.agent_runs.snapshots()
        assert len(rows) == 1
        assert rows[0]["status"] == "running"
        assert rows[0]["finishedAt"] is None
        manager.write_input(session_id, "release\n")
        final = receipt.wait(
            lambda items: len(items) == 1 and items[0]["status"] != "running"
        )
        assert final[0]["status"] in {"cancelled", "interrupted"}, {
            "runs": final,
            "process": manager.snapshot(session_id),
        }
        wait_for_output(manager, session_id, "CANCEL_DRAINED")
    finally:
        receipt.unsubscribe()
        manager.close()


@pytest.mark.parametrize("tool_name", ["command_exec", "python_exec"])
def test_mcp_launches_expose_real_sdk_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool_name: str
) -> None:
    monkeypatch.setenv("YOKE_USAGE_METRIC_LOG_DIR", str(tmp_path / "usage"))

    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        receipt = RegistryReceipt(service.runtime.manager)
        try:
            async with memory_client(service) as client:
                args: dict[str, Any] = {"mode": "background"}
                args.update(
                    {"argv": [sys.executable, str(WORKER)]}
                    if tool_name == "command_exec"
                    else {
                        "code": f"import runpy; runpy.run_path({str(WORKER)!r}, run_name='__main__')",
                        "timeout": 45,
                    }
                )
                launched = structured(await client.call_tool(tool_name, args))
                assert launched["ok"] is True, launched
                await asyncio.to_thread(receipt.wait, mixed_is_ready)
                listed = structured(
                    await client.call_tool(
                        "agent_runs", {"session_id": launched["session_id"]}
                    )
                )
                assert listed["ok"] is True, listed
                # Public MCP envelope uses data, matching HTTP's snapshot vocabulary.
                rows = listed["data"]
                assert mixed_is_ready(rows), listed
                assert "SECRET_" not in json.dumps(listed)
                released = structured(
                    await client.call_tool(
                        "process_input",
                        {
                            "session_id": launched["session_id"],
                            "chars": "release\n",
                            "wait_ms": 0,
                        },
                    )
                )
                assert released["ok"] is True
                await asyncio.to_thread(
                    receipt.wait,
                    lambda items: (
                        len(items) == 4 and all(r["status"] != "running" for r in items)
                    ),
                )
                finished = structured(
                    await client.call_tool("agent_runs", {"active_only": True})
                )
                assert finished["data"] == [], finished
        finally:
            receipt.unsubscribe()

    asyncio.run(scenario())
