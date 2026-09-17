"""Cancellation, backpressure and ownership regressions for process tools."""

from __future__ import annotations

from collections.abc import Iterator
import os
from pathlib import Path
import signal
import sys
import threading
import time
from typing import Any, cast

import pytest

from yoke.agent.loop.in_process_tool import (
    execute_in_process_tool,
    shutdown_in_process_tools,
)
from yoke.agent.tools import (
    CommandTool,
    ProcessCancelTool,
    ProcessInputTool,
    ProcessReadTool,
    PythonExecTool,
)
from yoke.agent.tools.command_process_manager import CommandProcessManager
from yoke.agent.tools.command_process_support import (
    admission,
    input as stdin_delivery,
    spawn,
)
from yoke.agent.tools.processes.models import ProcessCursor, ProcessReadRequest
from yoke.agent.tools.processes.observation import read_processes


@pytest.fixture
def manager() -> Iterator[CommandProcessManager]:
    manager = CommandProcessManager()
    try:
        yield manager
    finally:
        manager.close()


def tool(cls, manager, root):
    return cls.bind(root=root, command_process_manager=manager)


def start(manager, root, code, **arguments):
    return (
        tool(PythonExecTool, manager, root)
        .parse_arguments({"code": code, "mode": "background", **arguments})
        .execute()
    )


def wait_for(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.005)
    return bool(predicate())


def continuation(item: dict[str, Any]) -> dict[str, Any]:
    value: dict[str, Any] = {"session_id": item["session_id"]}
    if item.get("cursor") is not None:
        value["cursor"] = item["cursor"]
    return value


def test_native_stop_preserves_running_process_handle_and_cursor(manager, tmp_path):
    tools = {"python_exec": tool(PythonExecTool, manager, tmp_path)}
    try:
        result, stopped = execute_in_process_tool(
            tools=tools,
            name="python_exec",
            arguments={"code": "import time; time.sleep(60)"},
            stop_requested=lambda: bool(manager.snapshots()),
        )
        assert stopped and result["cancelled"] and result["running"]
        session_id = result["session_id"]
        cursor_value = result["cursor"]
        assert isinstance(session_id, int) and isinstance(cursor_value, str)
        cursor = ProcessCursor(session_id=session_id, cursor=cursor_value)
        assert cursor.session_id == result["session_id"]
        assert manager.snapshot(result["session_id"]).status == "running"
    finally:
        shutdown_in_process_tools(tools)


def test_native_stop_preserves_acknowledged_input_delivery(manager, tmp_path):
    running = start(
        manager, tmp_path, "input(); print('received', flush=True); input()"
    )
    tools = {"process_input": tool(ProcessInputTool, manager, tmp_path)}
    try:
        result, stopped = execute_in_process_tool(
            tools=tools,
            name="process_input",
            arguments={
                "session_id": running["session_id"],
                "chars": "go\n",
                "wait_ms": 5000,
            },
            stop_requested=lambda: (
                "received" in manager.snapshot(running["session_id"]).output_tail
            ),
        )
        assert stopped and result["cancelled"] and result["input_written"] is True
        assert result["session_id"] == running["session_id"]
        output = result["output"]
        assert isinstance(output, str) and "received" in output
        assert result["cursor"]
    finally:
        shutdown_in_process_tools(tools)


def test_execution_cancelled_while_queued_for_admission_never_starts(manager, tmp_path):
    cancelled = threading.Event()
    results = []
    bound = PythonExecTool.bind(
        root=tmp_path,
        command_process_manager=manager,
        cancel_requested=cancelled.is_set,
    )
    invocation = bound.parse_arguments(
        {"code": "from pathlib import Path; Path('unexpected').touch()"}
    )
    manager._spawn_lock.acquire()
    worker = threading.Thread(
        target=lambda: results.append(invocation.execute()), daemon=True
    )
    try:
        worker.start()
        cancelled.set()
        worker.join(timeout=1)
        assert not worker.is_alive()
        assert results[0]["cancelled"] and results[0]["session_id"] is None
    finally:
        manager._spawn_lock.release()
        worker.join(timeout=2)
    assert not (tmp_path / "unexpected").exists()
    assert not manager.snapshots()


def test_capacity_rejects_new_work_without_killing_an_existing_process(
    manager, tmp_path, monkeypatch
):
    monkeypatch.setattr(admission, "MAX_PROCESS_COUNT", 1)
    first = start(manager, tmp_path, "input()")
    for arguments in [{"argv": ["bad\x00name"]}, {"cmd": "echo harmless"}]:
        result = (
            tool(CommandTool, manager, tmp_path).parse_arguments(arguments).execute()
        )
        assert not result["ok"] and result["reason"] == "error"
        assert manager.snapshot(first["session_id"]).status == "running"
        snapshot = (
            tool(ProcessReadTool, manager, tmp_path)
            .parse_arguments({"sessions": [continuation(first)], "wait_ms": 0})
            .execute()
        )
        assert snapshot["ok"]


def test_blocked_stdin_delivery_is_cancellable_and_releases_its_lock(manager, tmp_path):
    running = start(manager, tmp_path, "import time; time.sleep(60)")
    cancelled = threading.Event()
    bound = ProcessInputTool.bind(
        root=tmp_path,
        command_process_manager=manager,
        cancel_requested=cancelled.is_set,
    )
    results = []
    worker = threading.Thread(
        target=lambda: results.append(
            bound.parse_arguments(
                {"session_id": running["session_id"], "chars": "x" * 2_000_000}
            ).execute()
        ),
        daemon=True,
    )
    worker.start()
    time.sleep(0.08)
    cancelled.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    result = results[0]
    assert result["cancelled"] and result["reason"] == "cancelled"
    assert result["input_written"] in {None, False}
    assert 0 <= result["input_bytes_written"] < 2_000_000
    managed = manager._get(running["session_id"])
    assert managed.input_lock.acquire(blocking=False)
    managed.input_lock.release()
    assert manager.snapshot(running["session_id"]).status == "running"


def test_stdin_backpressure_has_a_bounded_safety_deadline(
    manager, tmp_path, monkeypatch
):
    monkeypatch.setattr(stdin_delivery, "MAX_STDIN_DELIVERY_SECONDS", 0.05)
    running = start(manager, tmp_path, "import time; time.sleep(60)")
    result = (
        tool(ProcessInputTool, manager, tmp_path)
        .parse_arguments(
            {"session_id": running["session_id"], "chars": "x" * 2_000_000}
        )
        .execute()
    )
    assert not result["ok"] and result["reason"] == "error"
    assert result["input_written"] is None
    written = result["input_bytes_written"]
    assert isinstance(written, int) and written > 0


def test_stdin_retries_short_writes_without_losing_utf8_bytes(monkeypatch):
    read_fd, write_fd = os.pipe()
    delivered = bytearray()

    def short_write(fd, raw):
        assert fd == write_fd
        accepted = min(2, len(raw))
        delivered.extend(raw[:accepted])
        return accepted

    monkeypatch.setattr(stdin_delivery.os, "write", short_write)
    try:
        expected = "hello 🌍\n".encode()
        assert stdin_delivery.write_all(
            write_fd, expected, cancel_requested=None
        ) == len(expected)
        assert bytes(delivered) == expected
        with pytest.raises(OSError):
            os.fstat(write_fd)
    finally:
        os.close(read_fd)


@pytest.mark.skipif(os.name == "nt", reason="POSIX PTY descriptors")
def test_failed_pty_spawn_closes_both_allocated_descriptors(tmp_path, monkeypatch):
    import pty

    master, slave = pty.openpty()
    monkeypatch.setattr(pty, "openpty", lambda: (master, slave))

    def fail(*args, **kwargs):
        raise FileNotFoundError("injected failed spawn")

    monkeypatch.setattr(spawn.subprocess, "Popen", fail)
    with pytest.raises(FileNotFoundError):
        spawn.open_process(["missing"], tmp_path, os.environ.copy(), tty=True)
    for fd in (master, slave):
        with pytest.raises(OSError):
            os.fstat(fd)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process group ownership")
def test_finished_parent_cannot_leave_detached_stdio_descendant_running(
    manager, tmp_path
):
    child_code = "from pathlib import Path; import os,time; Path('child.pid').write_text(str(os.getpid())); time.sleep(60)"
    parent_code = (
        "import subprocess,sys,time; from pathlib import Path\n"
        f"subprocess.Popen([sys.executable,'-c',{child_code!r}], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "while not Path('child.pid').exists(): time.sleep(.005)\n"
    )
    result = (
        tool(PythonExecTool, manager, tmp_path)
        .parse_arguments({"code": parent_code})
        .execute()
    )
    child_pid = int((tmp_path / "child.pid").read_text())

    def alive():
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            return False
        stat = Path(f"/proc/{child_pid}/stat")
        try:
            return stat.read_text().split()[2] != "Z" if stat.exists() else True
        except FileNotFoundError:
            return False

    try:
        assert result["ok"] and not result["running"]
        assert wait_for(lambda: not alive())
        cancelled = (
            tool(ProcessCancelTool, manager, tmp_path)
            .parse_arguments({"session_id": result["session_id"]})
            .execute()
        )
        assert cancelled["ok"]
    finally:
        if alive():
            os.kill(child_pid, signal.SIGKILL)


def test_failed_close_wakes_long_read_and_keeps_cleanup_retryable(
    manager, tmp_path, monkeypatch
):
    running = start(manager, tmp_path, "input()")
    managed = manager._get(running["session_id"])
    results: list[dict[str, Any]] = []
    worker = threading.Thread(
        target=lambda: results.append(
            read_processes(
                manager,
                ProcessReadRequest(
                    sessions=[ProcessCursor.model_validate(continuation(running))],
                    wait_ms=3_600_000,
                ),
            )
        ),
        daemon=True,
    )
    worker.start()
    assert wait_for(lambda: bool(manager._listeners))
    with monkeypatch.context() as patcher:

        def fail():
            raise RuntimeError("injected cleanup failure")

        patcher.setattr(managed, "terminate", fail)
        with pytest.raises(RuntimeError, match="injected cleanup failure"):
            manager.close()
        worker.join(timeout=1)
        assert not worker.is_alive() and results[0]["reason"] == "error"


def test_execution_and_input_errors_have_explicit_bounded_reasons(manager, tmp_path):
    execution = (
        tool(CommandTool, manager, tmp_path)
        .parse_arguments({"argv": ["missing-" + "x" * 100_000]})
        .execute()
    )
    assert not execution["ok"] and execution["reason"] == "error"
    error = execution["error"]
    assert isinstance(error, str) and len(error) < 2200
    assert "argv" not in execution and "command" not in execution
    unknown = (
        tool(ProcessInputTool, manager, tmp_path)
        .parse_arguments({"session_id": 999999, "chars": "x"})
        .execute()
    )
    assert unknown["reason"] == "error" and unknown["input_written"] is False


def test_new_process_pages_preserve_crlf_and_bare_carriage_returns(manager, tmp_path):
    expected = "a\r\nb\rc\n"
    result = (
        tool(PythonExecTool, manager, tmp_path)
        .parse_arguments(
            {
                "code": f"import os; os.write(1, {expected.encode()!r})",
                "max_output_tokens": 1,
            }
        )
        .execute()
    )
    observed = result["output"]
    while result["has_more_output"]:
        read = (
            tool(ProcessReadTool, manager, tmp_path)
            .parse_arguments({"sessions": [continuation(result)], "wait_ms": 0})
            .execute()
        )
        result = cast(list[dict[str, Any]], read["items"])[0]
        observed += result["output"]
    assert observed == expected


@pytest.mark.skipif(os.name == "nt", reason="POSIX interactive PTY")
def test_interactive_pty_input_keeps_readers_alive_and_returns_final_cursor(
    manager, tmp_path
):
    running = (
        tool(CommandTool, manager, tmp_path)
        .parse_arguments(
            {
                "argv": [
                    sys.executable,
                    "-u",
                    "-c",
                    "print('ready', flush=True); value=input(); print('got:'+value)",
                ],
                "tty": True,
                "mode": "background",
            }
        )
        .execute()
    )
    read = (
        tool(ProcessReadTool, manager, tmp_path)
        .parse_arguments(
            {
                "sessions": [continuation(running)],
                "until": "output_or_completion",
                "wait_ms": 2000,
            }
        )
        .execute()
    )
    prompt = cast(list[dict[str, Any]], read["items"])[0]
    reply = (
        tool(ProcessInputTool, manager, tmp_path)
        .parse_arguments(
            {
                "session_id": running["session_id"],
                "chars": "hello\n",
                "cursor": prompt["cursor"],
                "wait_ms": 2000,
            }
        )
        .execute()
    )
    assert reply["ok"] and reply["input_written"]
    assert not reply["running"]
    output = reply["output"]
    assert isinstance(output, str) and "got:hello\r\n" in output
    assert not reply["has_more_output"] and reply["cursor"]
