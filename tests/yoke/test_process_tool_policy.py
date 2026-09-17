"""Public native process contracts, using short real child processes."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
import sys
import threading
import time
from typing import Any, cast

import pytest
from pydantic import ValidationError

from yoke.agent.tools import (
    CommandTool,
    LocalTool,
    ProcessCancelTool,
    ProcessInputTool,
    ProcessReadTool,
    PythonExecTool,
)
from yoke.agent.tools.command_process_manager import CommandProcessManager
from yoke.agent.tools.processes.cursor import encode_cursor, ProcessPosition
from yoke.agent.tools.processes.models import ProcessCursor, ProcessReadRequest
from yoke.agent.tools.processes.observation import read_processes


@pytest.fixture
def manager() -> Iterator[CommandProcessManager]:
    value = CommandProcessManager()
    try:
        yield value
    finally:
        value.close()


def invoke(
    cls: type[LocalTool], manager: CommandProcessManager, root: Path, **arguments: Any
) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        cls.bind(root=root, command_process_manager=manager)
        .parse_arguments(arguments)
        .execute(),
    )


def launch(
    manager: CommandProcessManager, root: Path, code: str, **options: Any
) -> dict[str, Any]:
    return invoke(
        PythonExecTool, manager, root, code=code, mode="background", **options
    )


def continuation(item: dict[str, Any]) -> dict[str, Any]:
    value: dict[str, Any] = {"session_id": item["session_id"]}
    if item.get("cursor") is not None:
        value["cursor"] = item["cursor"]
    return value


def read(
    manager: CommandProcessManager, root: Path, item: dict[str, Any], **options: Any
) -> dict[str, Any]:
    return invoke(
        ProcessReadTool, manager, root, sessions=[continuation(item)], **options
    )


@pytest.mark.parametrize("cls", [CommandTool, PythonExecTool])
def test_auto_returns_final_output_and_retains_handle(cls, manager, tmp_path):
    code = "print('done')"
    args = (
        {"code": code}
        if cls is PythonExecTool
        else {"argv": [sys.executable, "-c", code]}
    )
    result = invoke(cls, manager, tmp_path, **args)
    assert result["ok"] and not result["running"]
    assert result["reason"] == "completed"
    assert result["output"] == "done\n"
    assert result["exit_code"] == 0
    assert isinstance(result["session_id"], int)
    assert isinstance(result["cursor"], str) and result["cursor"].startswith("pc1_")
    assert not result["has_more_output"]
    assert read(manager, tmp_path, result, wait_ms=0)["items"][0]["output"] == ""


@pytest.mark.parametrize("cls", [CommandTool, PythonExecTool])
def test_background_is_immediate_and_wait_budget_can_be_increased(
    cls, manager, tmp_path
):
    code = "import time; time.sleep(0.3); print('finished')"
    args = (
        {"code": code}
        if cls is PythonExecTool
        else {"argv": [sys.executable, "-c", code]}
    )
    started = invoke(cls, manager, tmp_path, mode="background", **args)
    assert started["running"]
    assert started["reason"] == "snapshot"
    short = read(manager, tmp_path, started, wait_ms=20)
    assert short["reason"] == "deadline"
    assert short["items"][0]["running"]
    finished = read(manager, tmp_path, short["items"][0], wait_ms=2_000)
    assert finished["reason"] == "completed"
    assert finished["items"][0]["output"] == "finished\n"


def test_completion_does_not_return_on_chatty_output_or_page_limit(manager, tmp_path):
    started = launch(
        manager,
        tmp_path,
        "import time\nfor i in range(25):\n print('x'*200, flush=True)\n time.sleep(0.01)\n",
    )
    began = time.monotonic()
    result = read(manager, tmp_path, started, wait_ms=2_000, max_bytes=1024)
    assert result["reason"] == "completed"
    assert time.monotonic() - began >= 0.15
    item = result["items"][0]
    assert not item["running"] and item["has_more_output"]
    output = started["output"] + item["output"]
    while item["has_more_output"]:
        item = read(manager, tmp_path, item, wait_ms=0, max_bytes=1024)["items"][0]
        output += item["output"]
    assert output == ("x" * 200 + "\n") * 25


def test_completion_uses_one_deadline_and_waits_for_all_sessions(manager, tmp_path):
    first = launch(manager, tmp_path, "print('fast')")
    second = launch(manager, tmp_path, "import time; time.sleep(0.3); print('slow')")
    result = invoke(
        ProcessReadTool,
        manager,
        tmp_path,
        sessions=[continuation(first), continuation(second)],
        wait_ms=2000,
    )
    assert result["reason"] == "completed"
    assert [item["session_id"] for item in result["items"]] == [
        first["session_id"],
        second["session_id"],
    ]
    assert all(not item["running"] for item in result["items"])
    assert "slow" in result["items"][1]["output"]


def test_output_condition_returns_for_any_process_without_waiting_for_exit(
    manager, tmp_path
):
    quiet = launch(manager, tmp_path, "import time; time.sleep(60)")
    prompt = launch(
        manager, tmp_path, "import time; print('answer?', flush=True); time.sleep(60)"
    )
    result = invoke(
        ProcessReadTool,
        manager,
        tmp_path,
        sessions=[continuation(quiet), continuation(prompt)],
        until="output_or_completion",
        wait_ms=2000,
    )
    assert result["reason"] == "output"
    assert all(item["running"] for item in result["items"])
    assert prompt["output"] + result["items"][1]["output"] == "answer?\n"


def test_snapshot_and_deadline_never_terminate_work(manager, tmp_path):
    started = launch(manager, tmp_path, "import time; time.sleep(60)")
    snapshot = read(manager, tmp_path, started, wait_ms=0)
    assert snapshot["reason"] == "snapshot"
    result = read(manager, tmp_path, snapshot["items"][0], wait_ms=25)
    assert result["reason"] == "deadline"
    assert result["items"][0]["running"]
    assert manager.snapshot(started["session_id"]).status == "running"


def test_interactive_input_returns_only_output_after_supplied_cursor(manager, tmp_path):
    started = launch(
        manager,
        tmp_path,
        "print('name?', flush=True)\nname=input()\nprint('hello '+name, flush=True)",
    )
    prompt = read(
        manager, tmp_path, started, until="output_or_completion", wait_ms=2000
    )["items"][0]
    response = invoke(
        ProcessInputTool,
        manager,
        tmp_path,
        session_id=started["session_id"],
        chars="Daniel\n",
        cursor=prompt["cursor"],
        wait_ms=2000,
    )
    assert response["ok"] and response["input_written"]
    assert not response["running"]
    assert response["output"] == "hello Daniel\n"
    assert read(manager, tmp_path, response, wait_ms=0)["items"][0]["output"] == ""


def test_input_without_cursor_does_not_skip_unread_history(manager, tmp_path):
    started = launch(
        manager, tmp_path, "print('prompt', flush=True)\ninput()\nprint('answer')"
    )
    response = invoke(
        ProcessInputTool,
        manager,
        tmp_path,
        session_id=started["session_id"],
        chars="yes\n",
        wait_ms=2000,
    )
    assert response["output"] == "prompt\nanswer\n"


def test_invalid_input_cursor_is_rejected_before_writing(manager, tmp_path):
    started = launch(manager, tmp_path, "print(input(), flush=True)")
    invalid = encode_cursor(
        ProcessPosition(session_id=started["session_id"], after_seq=999_999)
    )
    failure = invoke(
        ProcessInputTool,
        manager,
        tmp_path,
        session_id=started["session_id"],
        chars="wrong\n",
        cursor=invalid,
    )
    assert not failure["ok"] and failure["input_written"] is False
    result = invoke(
        ProcessInputTool,
        manager,
        tmp_path,
        session_id=started["session_id"],
        chars="right\n",
        wait_ms=2000,
    )
    assert result["output"] == "right\n"


@pytest.mark.parametrize(
    "args",
    [
        {"session_id": 1, "chars": ""},
        {"session_id": 1},
        {"session_id": 1, "chars": "x", "wait_ms": 5001},
        {"session_id": 1, "chars": "x", "cursor": "bad"},
    ],
)
def test_input_request_rejects_empty_polling_and_invalid_arguments(args):
    with pytest.raises(ValidationError):
        ProcessInputTool.model_validate(args)


def test_cancelled_input_does_not_send_characters(manager, tmp_path):
    started = launch(manager, tmp_path, "print(input())")
    cancelled = (
        ProcessInputTool.bind(
            root=tmp_path,
            command_process_manager=manager,
            cancel_requested=lambda: True,
        )
        .parse_arguments({"session_id": started["session_id"], "chars": "wrong\n"})
        .execute()
    )
    assert cancelled["cancelled"] and cancelled["input_written"] is False
    result = invoke(
        ProcessInputTool,
        manager,
        tmp_path,
        session_id=started["session_id"],
        chars="right\n",
        wait_ms=2000,
    )
    assert result["output"] == "right\n"


def test_read_success_is_independent_of_process_exit_code(manager, tmp_path):
    failed = invoke(
        PythonExecTool, manager, tmp_path, code="print('failure'); raise SystemExit(7)"
    )
    assert not failed["ok"] and failed["exit_code"] == 7
    result = read(manager, tmp_path, failed)
    assert result["ok"] and result["items"][0]["ok"]
    assert result["items"][0]["exit_code"] == 7


def test_python_timeout_survives_later_reads(manager, tmp_path):
    started = launch(manager, tmp_path, "import time; time.sleep(60)", timeout=1)
    result = read(manager, tmp_path, started, wait_ms=2000)
    assert result["ok"] and result["reason"] == "completed"
    assert result["items"][0]["timed_out"] is True
    assert not result["items"][0]["running"]


def test_process_cancel_is_repeatable_and_preserves_history(manager, tmp_path):
    started = launch(
        manager, tmp_path, "import time; print('retained', flush=True); time.sleep(60)"
    )
    prompt = read(
        manager, tmp_path, started, until="output_or_completion", wait_ms=2000
    )
    assert prompt["ok"]
    first = invoke(
        ProcessCancelTool, manager, tmp_path, session_id=started["session_id"]
    )
    second = invoke(
        ProcessCancelTool, manager, tmp_path, session_id=started["session_id"]
    )
    assert first["ok"] and second["ok"] and not second["running"]
    assert "cursor" not in first and "output" not in first
    result = read(manager, tmp_path, prompt["items"][0], wait_ms=0)
    assert result["items"][0]["output"] == ""
    replay = read(manager, tmp_path, {"session_id": started["session_id"]}, wait_ms=0)
    assert replay["items"][0]["output"] == "retained\n"


def test_batch_lookup_failure_is_prompt_and_ordered(manager, tmp_path):
    started = launch(manager, tmp_path, "import time; time.sleep(60)")
    result = invoke(
        ProcessReadTool,
        manager,
        tmp_path,
        sessions=[{"session_id": 999999}, continuation(started)],
        wait_ms=3_600_000,
    )
    assert not result["ok"] and result["reason"] == "error"
    assert [item["session_id"] for item in result["items"]] == [
        999999,
        started["session_id"],
    ]
    assert result["items"][1]["running"]


def test_read_cancellation_stops_waiting_without_stopping_process(manager, tmp_path):
    started = launch(manager, tmp_path, "import time; time.sleep(60)")
    cancelled = threading.Event()
    timer = threading.Timer(0.05, cancelled.set)
    timer.start()
    try:
        result = read_processes(
            manager,
            ProcessReadRequest(
                sessions=[
                    ProcessCursor(
                        session_id=started["session_id"], cursor=started["cursor"]
                    )
                ]
            ),
            cancel_requested=cancelled.is_set,
        )
        assert result["reason"] == "cancelled" and not result["ok"]
        assert result["items"][0]["running"]
        assert manager.snapshot(started["session_id"]).status == "running"
    finally:
        timer.join()


def test_manager_close_wakes_event_waiters_without_cancellation_callback(
    manager, tmp_path
):
    started = launch(manager, tmp_path, "import time; time.sleep(60)")
    results: list[dict[str, Any]] = []
    worker = threading.Thread(
        target=lambda: results.append(
            read_processes(
                manager,
                ProcessReadRequest(
                    sessions=[
                        ProcessCursor(
                            session_id=started["session_id"], cursor=started["cursor"]
                        )
                    ],
                    wait_ms=3_600_000,
                ),
            )
        ),
        daemon=True,
    )
    worker.start()
    time.sleep(0.03)
    manager.close()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert results[0]["reason"] in {"error", "completed"}


def test_wait_limits_and_legacy_field_rejection():
    request = ProcessReadRequest(
        sessions=[ProcessCursor(session_id=1)], wait_ms=3_600_000
    )
    assert request.wait_ms == 3_600_000
    for arguments in [
        {"wait_ms": 3_600_001},
        {"wait_ms": -1},
        {"wait_ms": True},
        {"until": "batch"},
        {"yield_time_ms": 1000},
    ]:
        with pytest.raises(ValidationError):
            ProcessReadRequest.model_validate(
                {"sessions": [{"session_id": 1}], **arguments}
            )
    with pytest.raises(ValidationError):
        ProcessReadRequest.model_validate(
            {"sessions": [{"session_id": 1}, {"session_id": 1}]}
        )
    for cls, args in [
        (CommandTool, {"cmd": "echo hi"}),
        (PythonExecTool, {"code": "pass"}),
    ]:
        with pytest.raises(ValidationError):
            cls.model_validate({**args, "yield_time_ms": 1000})
        with pytest.raises(ValidationError):
            cls.model_validate({**args, "wait_ms": 1000})


def test_split_utf8_from_child_is_not_corrupted(manager, tmp_path):
    code = "import os,time; os.write(1,b'\\xf0\\x9f'); time.sleep(.03); os.write(1,b'\\x8c\\x8d\\n')"
    result = invoke(PythonExecTool, manager, tmp_path, code=code)
    assert result["output"] == "🌍\n"


def test_initial_execution_budget_keeps_undelivered_output_pageable(manager, tmp_path):
    expected = "🌍hello\n" * 300
    initial = invoke(
        PythonExecTool,
        manager,
        tmp_path,
        code=f"print({expected!r}, end='')",
        max_output_tokens=1,
    )
    assert initial["ok"] and not initial["running"] and initial["has_more_output"]
    assert len(initial["output"].encode()) == 4
    item, output = initial, initial["output"]
    while item["has_more_output"]:
        item = read(manager, tmp_path, item, wait_ms=0, max_bytes=1024)["items"][0]
        output += item["output"]
    assert output == expected


def test_input_response_budget_keeps_unread_bytes_pageable(manager, tmp_path):
    started = launch(manager, tmp_path, "input(); print('🌍hello' * 300)")
    response = invoke(
        ProcessInputTool,
        manager,
        tmp_path,
        session_id=started["session_id"],
        chars="go\n",
        cursor=started["cursor"],
        wait_ms=2000,
        max_output_tokens=1,
    )
    assert response["input_written"] and response["has_more_output"]
    item, output = response, response["output"]
    while item["has_more_output"]:
        item = read(manager, tmp_path, item, wait_ms=0, max_bytes=1024)["items"][0]
        output += item["output"]
    assert output == "🌍hello" * 300 + "\n"


def test_process_operations_survive_workspace_removal(manager, tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    started = launch(manager, root, "print(input(), flush=True); input()")
    root.rmdir()
    response = invoke(
        ProcessInputTool,
        manager,
        root,
        session_id=started["session_id"],
        chars="hello\n",
    )
    assert response["ok"] and response["output"] == "hello\n"
    snapshot = read(manager, root, response, wait_ms=0)
    assert snapshot["ok"] and snapshot["items"][0]["running"]
    cancelled = invoke(
        ProcessCancelTool, manager, root, session_id=started["session_id"]
    )
    assert cancelled["ok"] and not cancelled["running"]


def test_read_started_during_manager_shutdown_fails_without_waiting(manager, tmp_path):
    started = launch(manager, tmp_path, "input()")
    # close() sets this before it drains child processes and removes their records.
    with manager._lock:
        manager._closed = True
    result = read_processes(
        manager,
        ProcessReadRequest(
            sessions=[
                ProcessCursor(
                    session_id=started["session_id"], cursor=started["cursor"]
                )
            ],
            wait_ms=3_600_000,
        ),
    )
    assert result["reason"] == "error" and not result["ok"]
    assert "closed" in result["items"][0]["error"]
