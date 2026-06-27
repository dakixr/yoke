from __future__ import annotations

from pathlib import Path
import shlex
import sys
import threading
import time
from typing import Any, cast


from yoke.agent.tools import (
    CommandTool,
    CommandProcessManager,
    EditTool,
    ExecCommandTool,
    ExtractFileContextTool,
    GrepTool,
    LocalTool,
    PythonExecTool,
    ReadTool,
    WriteStdinTool,
    COMMAND_TOOL_NAME,
)
from yoke.agent.tools.shell import build_shell_command


def as_dict(value: object) -> dict[str, Any]:
    return cast(dict[str, Any], value)


def tool_set(tmp_path: Path, *, cancel_requested=None) -> list[LocalTool]:
    manager = CommandProcessManager()
    return [
        ReadTool.bind(root=tmp_path, cancel_requested=cancel_requested),
        CommandTool.bind(
            root=tmp_path,
            cancel_requested=cancel_requested,
            command_process_manager=manager,
        ),
        WriteStdinTool.bind(
            root=tmp_path,
            cancel_requested=cancel_requested,
            command_process_manager=manager,
        ),
        PythonExecTool.bind(root=tmp_path, cancel_requested=cancel_requested),
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
    assert "oldString" in definitions["edit"]["parameters"]["properties"]
    assert "old_string" not in definitions["edit"]["parameters"]["properties"]
    assert "newString" in definitions["edit"]["parameters"]["properties"]
    assert "occurrence" not in definitions["edit"]["parameters"]["properties"]
    assert "replaceAll" in definitions["edit"]["parameters"]["properties"]


def test_tools_allow_paths_outside_root(tmp_path: Path) -> None:
    tools = tool_set(tmp_path)
    outside = tmp_path.parent / "escape.txt"
    outside.write_text("outside", encoding="utf-8")

    result = as_dict(execute_tool(tools, "read", {"path": "../escape.txt"}))

    assert result["ok"] is True
    assert result["content"] == "outside"


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


def test_command_tool_turn_cancel_leaves_background_process_running(
    tmp_path: Path,
) -> None:
    stop_event = threading.Event()
    tools = tool_set(tmp_path, cancel_requested=stop_event.is_set)
    command = f'{shlex.quote(sys.executable)} -c "import time; time.sleep(2)"'

    def request_stop() -> None:
        time.sleep(0.1)
        stop_event.set()

    stopper = threading.Thread(target=request_stop, daemon=True)
    stopper.start()
    result = as_dict(execute_tool(tools, COMMAND_TOOL_NAME, {"command": command}))
    stopper.join(timeout=1)

    assert result["ok"] is True
    assert result["running"] is True
    assert isinstance(result["session_id"], int)
    command_tool = next(tool for tool in tools if tool.name == COMMAND_TOOL_NAME)
    manager = cast(
        CommandProcessManager,
        command_tool._context["command_process_manager"],
    )
    assert manager.terminate_process(cast(int, result["session_id"])) is True


def test_command_tool_is_universally_named_exec_command_and_runs_zsh() -> None:
    assert COMMAND_TOOL_NAME == "exec_command"
    env = {"YOKE_ZSH": "zsh"}
    command = build_shell_command("echo ok", env)

    assert command[0] == "zsh"
    assert command[1:3] == ["-l", "-c"]
    assert env["YOKE_COMMAND_TOOL_COMMAND"] == "echo ok"


def test_command_tool_keeps_universal_name_on_windows(monkeypatch) -> None:
    import importlib
    import yoke.agent.tools.shell as shell

    real_os_name = shell.os.name
    monkeypatch.setattr(shell.os, "name", "nt")
    try:
        assert importlib.reload(shell).COMMAND_TOOL_NAME == "exec_command"
    finally:
        monkeypatch.setattr(shell.os, "name", real_os_name)
        importlib.reload(shell)


def test_build_shell_command_uses_powershell_on_windows(monkeypatch) -> None:
    import yoke.agent.tools.shell as shell

    monkeypatch.setattr(shell.os, "name", "nt")
    monkeypatch.setattr(shell.shutil, "which", lambda name: None)
    env = {"ComSpec": r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"}

    command = shell.build_shell_command(
        '"C:\\Program Files\\Python\\python.exe" -V && echo ok', env
    )

    assert command[:5] == [
        env["ComSpec"],
        "-NoLogo",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
    ]
    assert env["YOKE_COMMAND_TOOL_COMMAND"] == (
        '& "C:\\Program Files\\Python\\python.exe" -V ; echo ok'
    )


def test_build_shell_command_uses_cmd_fallback_on_windows(monkeypatch) -> None:
    import yoke.agent.tools.shell as shell

    monkeypatch.setattr(shell.os, "name", "nt")
    monkeypatch.setattr(shell.shutil, "which", lambda name: None)

    command = shell.build_shell_command("echo ok", {"ComSpec": "cmd.exe"})

    assert command == ["cmd.exe", "/d", "/s", "/c", "echo ok"]


def test_command_tool_exposes_current_python_as_python_commands(tmp_path: Path) -> None:
    tools = tool_set(tmp_path)
    result = as_dict(
        execute_tool(
            tools,
            COMMAND_TOOL_NAME,
            {
                "command": (
                    "python -c 'import sys; print(sys.executable)' && "
                    "python3 -c 'import sys; print(sys.executable)'"
                )
            },
        )
    )

    assert result["ok"] is True
    assert result["exit_code"] == 0
    assert result["running"] is False
    assert isinstance(result["wall_time_seconds"], float)
    assert result["wall_time_seconds"] >= 0
    assert "content" not in cast(dict[str, object], result["outputTruncationDetails"])
    assert "stdout" not in result
    assert "stderr" not in result
    assert "details" not in result
    lines = cast(str, result["output"]).splitlines()
    assert lines == [sys.executable, sys.executable]


def test_command_tool_streams_output_chunks(tmp_path: Path) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    tool = CommandTool.bind(
        root=tmp_path,
        tool_event=lambda event, payload: events.append((event, payload)),
    )

    result = as_dict(
        tool.parse_arguments(
            {
                "command": (
                    f"{shlex.quote(sys.executable)} -c "
                    '\'import sys; print("out"); print("err", file=sys.stderr)\''
                )
            }
        ).execute()
    )

    assert result["ok"] is True
    streamed = {
        (cast(str, payload["stream"]), cast(str, payload["text"]))
        for event, payload in events
        if event == "tool_execution_output_delta"
    }
    assert ("stdout", "out\n") in streamed
    assert ("stderr", "err\n") in streamed


def test_command_tool_yields_and_write_stdin_polls_to_completion(
    tmp_path: Path,
) -> None:
    tools = tool_set(tmp_path)
    started = as_dict(
        execute_tool(
            tools,
            COMMAND_TOOL_NAME,
            {
                "cmd": (
                    f"{shlex.quote(sys.executable)} -u -c "
                    '\'import time; print("started"); time.sleep(0.6); print("done")\''
                ),
                "yield_time_ms": 50,
            },
        )
    )

    assert started["ok"] is True
    assert started["running"] is True
    assert isinstance(started["session_id"], int)

    completed = as_dict(
        execute_tool(
            tools,
            "write_stdin",
            {
                "session_id": started["session_id"],
                "yield_time_ms": 5000,
            },
        )
    )

    assert completed["ok"] is True
    assert completed["running"] is False
    assert completed["session_id"] is None
    assert completed["exit_code"] == 0
    combined_output = (
        cast(str, started["output"]) + "\n" + cast(str, completed["output"])
    )
    assert "started" in combined_output
    assert "done" in combined_output


def test_write_stdin_interacts_with_tty_session(tmp_path: Path) -> None:
    tools = tool_set(tmp_path)
    started = as_dict(
        execute_tool(
            tools,
            COMMAND_TOOL_NAME,
            {
                "cmd": "read value; echo received:$value",
                "tty": True,
                "yield_time_ms": 50,
            },
        )
    )

    completed = as_dict(
        execute_tool(
            tools,
            "write_stdin",
            {
                "session_id": started["session_id"],
                "chars": "hello\n",
                "yield_time_ms": 5000,
            },
        )
    )

    assert completed["exit_code"] == 0
    assert "received:hello" in cast(str, completed["output"])


def test_exec_command_schema_uses_new_public_api(tmp_path: Path) -> None:
    definition = ExecCommandTool.bind(root=tmp_path).to_definition()
    function = cast(dict[str, object], definition["function"])
    parameters = cast(dict[str, object], function["parameters"])
    properties = cast(dict[str, object], parameters["properties"])

    assert function["name"] == "exec_command"
    assert "cmd" in properties
    assert "command" not in properties
    assert "timeout" not in properties


def test_python_exec_exposes_current_python_to_subprocesses(tmp_path: Path) -> None:
    tools = tool_set(tmp_path)
    result = as_dict(
        execute_tool(
            tools,
            "python_exec",
            {
                "code": (
                    "import os, subprocess, sys\n"
                    "print(sys.executable)\n"
                    "print(os.environ['YOKE_PYTHON_EXECUTABLE'])\n"
                    "print(subprocess.check_output(["
                    "'python', '-c', 'import sys; print(sys.executable)'"
                    "], text=True).strip())\n"
                    "print(subprocess.check_output(["
                    "'python3', '-c', 'import sys; print(sys.executable)'"
                    "], text=True).strip())"
                )
            },
        )
    )

    assert result["ok"] is True
    assert result["python_executable"] == sys.executable
    lines = cast(str, result["output"]).splitlines()
    assert lines == [sys.executable] * 4


def test_python_exec_streams_output_chunks(tmp_path: Path) -> None:
    events: list[tuple[str, dict[str, object]]] = []
    tool = PythonExecTool.bind(
        root=tmp_path,
        tool_event=lambda event, payload: events.append((event, payload)),
    )

    result = as_dict(
        tool.parse_arguments(
            {"code": "import sys\nprint('out')\nprint('err', file=sys.stderr)"}
        ).execute()
    )

    assert result["ok"] is True
    assert (
        "tool_execution_output_delta",
        {"stream": "stdout", "text": "out\n"},
    ) in events
    assert (
        "tool_execution_output_delta",
        {"stream": "stderr", "text": "err\n"},
    ) in events


def test_command_tool_preserves_quoted_heredoc_content(tmp_path: Path) -> None:
    tools = tool_set(tmp_path)
    result = as_dict(
        execute_tool(
            tools,
            COMMAND_TOOL_NAME,
            {
                "command": (
                    "cat > plan.md <<'EOF'\n"
                    "# Plan: Scoped `/tools` Changes\n"
                    "1. `in_progress` Inspect the current `/tools` selector.\n"
                    "EOF"
                )
            },
        )
    )

    assert result["ok"] is True
    assert (tmp_path / "plan.md").read_text(encoding="utf-8") == (
        "# Plan: Scoped `/tools` Changes\n"
        "1. `in_progress` Inspect the current `/tools` selector.\n"
    )


def test_grep_truncates_long_matched_lines(tmp_path: Path) -> None:
    tools = [GrepTool.bind(root=tmp_path)]
    long_line = "needle " + ("x" * 800)
    (tmp_path / "search.txt").write_text(long_line, encoding="utf-8")

    result = as_dict(execute_tool(tools, "grep", {"path": ".", "pattern": "needle"}))

    assert result["ok"] is True
    files = cast(list[dict[str, Any]], result["files"])
    assert result["match_count"] == 1
    assert files[0]["path"] == "search.txt"
    matches = cast(list[dict[str, Any]], files[0]["matches"])
    assert matches[0]["line_truncated"] is True
    assert cast(str, matches[0]["text"]).endswith("[truncated]")


def test_extract_file_context_reads_text_files(tmp_path: Path) -> None:
    tool = ExtractFileContextTool.bind(root=tmp_path)
    (tmp_path / "notes.txt").write_text("alpha\nbeta\n", encoding="utf-8")

    result = as_dict(tool.parse_arguments({"path": "notes.txt"}).execute())

    assert result["ok"] is True
    assert result["extractor"] == "text"
    assert result["content"] == "alpha\nbeta\n"


def test_extract_file_context_reports_unsupported_binary_files(
    tmp_path: Path,
) -> None:
    tool = ExtractFileContextTool.bind(root=tmp_path)
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02\x03")

    result = as_dict(tool.parse_arguments({"path": "blob.bin"}).execute())

    assert result["ok"] is True
    assert result["extractor"] == "binary"
    assert "Unsupported binary file" in cast(str, result["content"])


def test_extract_file_context_can_be_used_as_explicit_tool(
    tmp_path: Path,
) -> None:
    (tmp_path / "notes.txt").write_text("a\nb\n", encoding="utf-8")
    tools = [
        ReadTool.bind(root=tmp_path),
        ExtractFileContextTool.bind(root=tmp_path),
    ]

    result = as_dict(execute_tool(tools, "extract_file_context", {"path": "notes.txt"}))

    assert result["ok"] is True
    assert result["extractor"] == "text"
