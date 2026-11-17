"""Native process input validation and wait policy."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from pydantic import ValidationError

from yoke.agent.tools import (
    CommandTool,
    ProcessInputTool,
    PythonExecTool,
)
from yoke.agent.tools.command_process import CommandProcessManager
from yoke.agent.tools.processes.models import ProcessCursor, ProcessReadRequest


@pytest.fixture
def manager() -> Iterator[CommandProcessManager]:
    value = CommandProcessManager()
    try:
        yield value
    finally:
        value.close()


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
