from __future__ import annotations

# ruff: noqa: D100, D103, S101

from pathlib import Path
import shlex
import subprocess
import sys
import threading
import time
from typing import Any, cast

import pytest
from pydantic import ValidationError

from yoke.agent.tools import (
    AttachImageTool,
    CommandTool,
    EditTool,
    LocalTool,
    PythonExecTool,
    ReadTool,
    WriteStdinTool,
    COMMAND_TOOL_NAME,
)
from yoke.agent.models import MessageImageURLContentPart
from yoke.agent.tools.command_process_types import (
    CommandProcessResult,
    clamp_exec_yield_time,
    clamp_write_yield_time,
)
from yoke.agent.tools.command_process_manager import (
    CommandProcessManager,
)


def as_dict(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value)


def tool_set(tmp_path: Path, *, cancel_requested=None) -> list[LocalTool]:
    return [
        ReadTool.bind(root=tmp_path, cancel_requested=cancel_requested),
        CommandTool.bind(root=tmp_path, cancel_requested=cancel_requested),
        WriteStdinTool.bind(root=tmp_path, cancel_requested=cancel_requested),
        EditTool.bind(root=tmp_path, cancel_requested=cancel_requested),
    ]


def execute_tool(
    tools: list[LocalTool], name: str, arguments: dict[str, object]
) -> dict[str, object]:
    for tool in tools:
        if tool.name == name:
            return tool.parse_arguments(arguments).execute()
    return {"ok": False, "error": f"Unknown tool: {name}"}


def test_tools_expose_pydantic_definitions(tmp_path: Path) -> None:
    tools = [ReadTool.bind(root=tmp_path), EditTool.bind(root=tmp_path)]
    definitions = {
        tool["function"]["name"]: tool["function"]
        for tool in cast(list[dict[str, Any]], [tool.to_definition() for tool in tools])
    }

    assert sorted(definitions) == ["edit", "read"]
    assert "offset" in definitions["read"]["parameters"]["properties"]
    assert "oldText" in definitions["edit"]["parameters"]["properties"]
    assert "old_text" not in definitions["edit"]["parameters"]["properties"]
    assert "occurrence" in definitions["edit"]["parameters"]["properties"]
    assert "replaceAll" in definitions["edit"]["parameters"]["properties"]


def test_attach_image_keeps_base64_out_of_tool_result(tmp_path: Path) -> None:
    image_path = tmp_path / "preview.png"
    image_path.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
        b"\x1f\x15\xc4\x89\x00\x00\x00\rIDAT\x08\xd7c\xf8\xcf\xc0\xf0\x1f"
        b"\x00\x05\x00\x01\xff\x89\x99=\x1d\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    tool = AttachImageTool.bind(root=tmp_path, messages=[])
    invocation = tool.parse_arguments(
        {"path": str(image_path), "caption": "Inspect this image"}
    )

    result = invocation.execute()
    pending = invocation.pending_context_messages(result)

    assert "context_messages" not in result
    assert len(str(result)) < 1_000
    assert len(pending) == 1
    assert isinstance(pending[0].content, list)
    image_part = pending[0].content[-1]
    assert isinstance(image_part, MessageImageURLContentPart)
    assert image_part.image_url.url.startswith("data:image/png;base64,")


def test_read_defaults_to_first_150_lines_and_reports_next_offset(
    tmp_path: Path,
) -> None:
    tools = tool_set(tmp_path)
    lines = "\n".join(f"line {index}" for index in range(2505))
    (tmp_path / "large.txt").write_text(lines, encoding="utf-8")

    result = as_dict(execute_tool(tools, "read", {"path": "large.txt"}))

    assert result["ok"] is True
    assert result["offset"] == 1
    assert result["limit"] == 150
    assert result["next_offset"] == 151
    assert "Use offset=151 to continue." in result["content"]
    assert "details" not in result


def test_command_tool_can_be_cancelled(tmp_path: Path) -> None:
    stop_event = threading.Event()
    stop_event.set()
    tools = tool_set(tmp_path, cancel_requested=stop_event.is_set)
    command = f'{shlex.quote(sys.executable)} -c "import time; time.sleep(2)"'
    result = as_dict(execute_tool(tools, COMMAND_TOOL_NAME, {"command": command}))

    assert result["ok"] is False
    assert result["cancelled"] is True
    assert result["error"] == "Command cancelled"


def test_command_tool_defaults_to_thirty_second_yield(tmp_path: Path) -> None:
    tool = CommandTool.bind(root=tmp_path)

    parsed = cast(CommandTool, tool.parse_arguments({"cmd": "echo ready"}))
    definition = cast(dict[str, Any], tool.to_definition())
    properties = definition["function"]["parameters"]["properties"]

    assert parsed.yield_time_ms == 30_000
    assert properties["yield_time_ms"]["default"] == 30_000
    assert properties["yield_time_ms"]["maximum"] == 300_000


def test_exec_yield_honors_the_documented_five_minute_limit() -> None:
    assert clamp_exec_yield_time(300_000) == 300_000
    assert clamp_exec_yield_time(500_000) == 300_000


def test_command_manager_passes_five_minute_wait_to_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    waits: list[int] = []

    class FinishedCommand:
        session_id = 91_001

        def wait_and_consume(
            self,
            yield_time_ms: int,
            *,
            cancel_requested: object,
        ) -> CommandProcessResult:
            del cancel_requested
            waits.append(yield_time_ms)
            return CommandProcessResult(
                session_id=None,
                exit_code=0,
                output="done",
                wall_time_seconds=0,
                original_output_bytes=4,
            )

    manager = CommandProcessManager()
    monkeypatch.setattr(manager, "_spawn", lambda *_args, **_kwargs: FinishedCommand())

    result = manager.exec_command(
        command="ignored",
        cwd=tmp_path,
        tty=False,
        yield_time_ms=300_000,
        shell=None,
        login=True,
        cancel_requested=None,
    )

    assert result.exit_code == 0
    assert waits == [300_000]


def test_command_tool_direct_argv_bypasses_shell_parsing(tmp_path: Path) -> None:
    manager = CommandProcessManager()
    tool = CommandTool.bind(root=tmp_path, command_process_manager=manager)
    try:
        parsed = cast(
            CommandTool,
            tool.parse_arguments(
                {
                    "argv": [
                        sys.executable,
                        "-c",
                        "import sys; print(sys.argv[1])",
                        "literal && shell | syntax",
                    ]
                }
            ),
        )
        result = as_dict(parsed.execute())
        parameters = cast(dict[str, Any], tool.to_definition())["function"][
            "parameters"
        ]
        properties = parameters["properties"]

        assert result["ok"] is True
        assert result["output"] == "literal && shell | syntax"
        assert "argv" in properties
        assert parameters["anyOf"] == [
            {"required": ["cmd"]},
            {"required": ["argv"]},
        ]
    finally:
        manager.close()


def test_command_tool_requires_exactly_one_execution_mode(tmp_path: Path) -> None:
    tool = CommandTool.bind(root=tmp_path)

    with pytest.raises(ValidationError, match="exactly one of cmd or argv"):
        tool.parse_arguments({})
    with pytest.raises(ValidationError, match="exactly one of cmd or argv"):
        tool.parse_arguments({"cmd": "echo one", "argv": ["echo", "two"]})


def test_python_exec_defaults_to_thirty_second_yield(tmp_path: Path) -> None:
    tool = PythonExecTool.bind(root=tmp_path)

    parsed = cast(PythonExecTool, tool.parse_arguments({"code": "pass"}))
    definition = cast(dict[str, Any], tool.to_definition())
    properties = definition["function"]["parameters"]["properties"]

    assert parsed.yield_time_ms == 30_000
    assert properties["yield_time_ms"]["default"] == 30_000


def test_python_exec_streams_output_through_write_stdin(
    tmp_path: Path,
) -> None:
    manager = CommandProcessManager()
    python_tool = PythonExecTool.bind(root=tmp_path, command_process_manager=manager)
    stdin_tool = WriteStdinTool.bind(root=tmp_path, command_process_manager=manager)
    try:
        started = as_dict(
            python_tool.parse_arguments(
                {
                    "code": (
                        "import time\nprint('first')\ntime.sleep(2)\nprint('second')"
                    ),
                    "yield_time_ms": 1_000,
                }
            ).execute()
        )

        assert started["running"] is True
        assert started["session_id"] is not None
        assert "first" in started["output"]
        completed = as_dict(
            stdin_tool.parse_arguments(
                {
                    "session_id": started["session_id"],
                    "yield_time_ms": 3_000,
                }
            ).execute()
        )

        assert completed["ok"] is True
        assert completed["running"] is False
        assert "second" in completed["output"]
    finally:
        manager.close()


def test_python_exec_timeout_applies_after_yield(tmp_path: Path) -> None:
    manager = CommandProcessManager()
    tool = PythonExecTool.bind(root=tmp_path, command_process_manager=manager)
    try:
        result = as_dict(
            tool.parse_arguments(
                {
                    "code": "import time\nprint('ready')\ntime.sleep(5)",
                    "yield_time_ms": 2_000,
                    "timeout": 1,
                }
            ).execute()
        )

        assert result["ok"] is False
        assert result["timed_out"] is True
        assert result["running"] is False
        assert "ready" in result["output"]
        assert result["error"] == "Python execution timed out after 1 seconds"
    finally:
        manager.close()


def test_command_process_close_waits_for_in_flight_spawn(
    tmp_path: Path, monkeypatch
) -> None:
    import yoke.agent.tools.command_process_manager as manager_module

    opening = threading.Event()
    allow_open = threading.Event()

    class FinishedProcess:
        pid = 2_000_000_000
        stdin = None
        stdout = None
        stderr = None

        def poll(self) -> int:
            return 0

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return 0

    def delayed_open_process(
        argv: list[str] | str,
        cwd: Path,
        env: dict[str, str],
        *,
        tty: bool,
    ) -> tuple[subprocess.Popen[bytes], int | None, int | None]:
        del argv, cwd, env, tty
        opening.set()
        assert allow_open.wait(timeout=5)
        return cast(subprocess.Popen[bytes], FinishedProcess()), None, None

    monkeypatch.setattr(manager_module, "_open_process", delayed_open_process)
    manager = CommandProcessManager()
    errors: list[BaseException] = []

    def execute() -> None:
        try:
            manager.exec_command(
                command="ignored",
                cwd=tmp_path,
                tty=False,
                yield_time_ms=250,
                shell=None,
                login=True,
                cancel_requested=None,
            )
        except BaseException as exc:  # pragma: no cover - assertion below
            errors.append(exc)

    worker = threading.Thread(target=execute)
    worker.start()
    assert opening.wait(timeout=5)
    closer = threading.Thread(target=manager.close)
    closer.start()
    time.sleep(0.05)
    assert closer.is_alive()
    allow_open.set()
    worker.join(timeout=10)
    closer.join(timeout=10)

    assert not worker.is_alive()
    assert not closer.is_alive()
    assert errors == []
    assert manager.snapshots() == []
    with pytest.raises(RuntimeError, match="manager is closed"):
        manager.acquire()


def test_final_release_is_atomic_with_acquire(monkeypatch) -> None:
    manager = CommandProcessManager().acquire()
    original_close = manager.close
    closing = threading.Event()
    allow_close = threading.Event()
    errors: list[BaseException] = []

    def delayed_close() -> None:
        closing.set()
        assert allow_close.wait(timeout=5)
        original_close()

    def acquire() -> None:
        try:
            manager.acquire()
        except BaseException as exc:  # pragma: no cover - assertion below
            errors.append(exc)

    monkeypatch.setattr(manager, "close", delayed_close)
    releaser = threading.Thread(target=manager.release)
    releaser.start()
    assert closing.wait(timeout=5)
    acquirer = threading.Thread(target=acquire)
    acquirer.start()
    time.sleep(0.05)
    assert acquirer.is_alive()
    allow_close.set()
    releaser.join(timeout=5)
    acquirer.join(timeout=5)

    assert not releaser.is_alive()
    assert not acquirer.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)


def test_write_stdin_poll_yield_clamps_to_one_hour() -> None:
    assert clamp_write_yield_time(3_700_000, has_input=False) == 3_600_000
    assert clamp_write_yield_time(300_000, has_input=True) == 30_000


def test_write_stdin_schema_accepts_one_hour_poll(tmp_path: Path) -> None:
    tool = WriteStdinTool.bind(root=tmp_path)

    parsed = cast(
        WriteStdinTool,
        tool.parse_arguments(
            {"session_id": 1, "chars": "", "yield_time_ms": 3_600_000}
        ),
    )

    assert parsed.yield_time_ms == 3_600_000


def test_write_tool_creates_text_file(tmp_path: Path) -> None:
    from yoke.agent.tools import WriteTool

    tool = WriteTool.bind(root=tmp_path)
    result = as_dict(
        tool.parse_arguments(
            {
                "path": "nested/notes.txt",
                "content": "hello\n",
                "createDirs": True,
            }
        ).execute()
    )

    assert result["ok"] is True
    assert result["created"] is True
    assert (tmp_path / "nested" / "notes.txt").read_text(encoding="utf-8") == "hello\n"
