"""Real SDK subprocesses across managed command, Python, and MCP entry points."""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import shlex
import sys
import threading
from typing import cast

import pytest

from yoke.agent.tools.command import ExecCommandTool
from yoke.agent.tools.command_process import CommandProcessManager
from yoke.agent.tools.python_exec import PythonExecTool
from yoke.ai.providers.usage_context import usage_metric_context


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


def test_concurrent_hosts_cannot_cross_attribute_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("YOKE_DB_PATH", str(tmp_path / "usage" / "yoke.db"))

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


def test_worker_crash_does_not_claim_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("YOKE_DB_PATH", str(tmp_path / "usage" / "yoke.db"))
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
