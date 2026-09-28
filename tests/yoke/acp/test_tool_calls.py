"""ACP display projection must not change native inputs, results, or lifecycle."""

from __future__ import annotations

import copy
import json
import shlex
from typing import Any

import pytest
from acp import schema as s

from yoke.acp.tools import ToolCallProjector
from yoke.acp.tools.output import DISPLAY_BYTES, output_content


def project(
    name: str,
    arguments: Any,
    result: Any = None,
    *,
    ok: bool = True,
) -> tuple[dict[str, Any], dict[str, Any]]:
    tools = ToolCallProjector()
    start = tools.update(
        start=True,
        data={
            "tool_call_id": "call",
            "tool_name": name,
            "tool_arguments": json.dumps(arguments),
        },
    )
    end = tools.update(
        start=False,
        data={
            "tool_call_id": "call",
            "tool_name": name,
            "executed_arguments": arguments,
            "result": result,
            "ok": ok,
        },
    )
    for update in (start, end):
        s.SessionNotification.model_validate({"sessionId": "session", "update": update})
    return start, end


@pytest.mark.parametrize(
    ("arguments", "command"),
    [
        ({"cmd": "git status --short", "workdir": "/repo"}, "git status --short"),
        ({"command": "printf 'one\\ntwo\\n'"}, "printf 'one\\ntwo\\n'"),
        (
            {"argv": ["python", "-c", "print('hello world')"]},
            shlex.join(["python", "-c", "print('hello world')"]),
        ),
        ({"cmd": "printf '%s' `pwd`\nprintf done"}, "printf '%s' `pwd`\nprintf done"),
    ],
)
def test_command_metadata_survives_completion(
    arguments: dict[str, Any], command: str
) -> None:
    start, end = project("command_exec", arguments, {"ok": True, "output": "Done.\n"})
    assert start["kind"] == end["kind"] == "execute"
    assert start["title"] == end["title"]
    assert "\n" not in start["title"]
    assert start["rawInput"]["command"] == end["rawInput"]["command"] == command
    assert start["status"] == "in_progress"
    assert end["status"] == "completed"
    assert end["content"][0]["content"]["text"] == "Done.\n"


def test_python_keeps_full_script_out_of_compact_identity() -> None:
    code = "print('hello')\n" * 1000
    start, end = project(
        "python_exec", {"code": code, "python_executable": "/venv/bin/python"}
    )
    assert start["kind"] == end["kind"] == "execute"
    assert start["rawInput"]["code"] == code
    assert start["rawInput"]["command"] == "/venv/bin/python"
    assert len(start["title"]) < 160


@pytest.mark.parametrize(
    ("name", "arguments", "kind"),
    [
        ("read", {"path": "/repo/app.py", "offset": 7}, "read"),
        ("read_file", {"path": "/repo/app.py"}, "read"),
        ("edit", {"path": "/repo/app.py", "new_text": "fragment"}, "edit"),
        ("write", {"path": "/repo/app.py", "content": "new"}, "edit"),
        ("edit", {"path": "/repo/app.py", "delete_file": True}, "delete"),
    ],
)
def test_file_kinds_and_absolute_locations(
    name: str, arguments: dict[str, Any], kind: str
) -> None:
    start, end = project(name, arguments)
    assert start["kind"] == end["kind"] == kind
    assert start["locations"] == end["locations"]
    assert start["locations"][0]["path"] == "/repo/app.py"
    if "offset" in arguments:
        assert start["locations"][0]["line"] == 7
    # Replacement fragments are not complete old/new file contents for ACP diffs.
    assert end["content"] == []


def test_patch_exposes_targets_without_fabricating_file_contents() -> None:
    patch = (
        "*** Begin Patch\n*** Update File: /repo/old.py\n"
        "*** Move to: /repo/new.py\n@@\n-old\n+new\n"
        "*** Add File: relative.py\n+line\n*** End Patch"
    )
    start, end = project("apply_patch", {"input": patch})
    assert start["rawInput"] == {"input": patch, "path": "/repo/old.py"}
    assert start["locations"] == [{"path": "/repo/old.py"}, {"path": "/repo/new.py"}]
    assert start["kind"] == end["kind"] == "edit"


def test_relative_paths_are_not_resolved_against_the_peer_workspace() -> None:
    start, end = project("read", {"path": "src/app.py", "offset": 9})
    assert start["rawInput"]["path"] == "src/app.py"
    assert start["locations"] == end["locations"] == []


@pytest.mark.parametrize(
    ("name", "arguments", "query"),
    [
        ("rg", {"patterns": ["tool_call", "rawOutput"]}, "tool_call | rawOutput"),
        ("fd", {"pattern": "AGENTS.md"}, "AGENTS.md"),
        ("rg", {"mode": "files", "paths": ["src", "tests"]}, "src, tests"),
        ("fd", {}, "files"),
    ],
)
def test_searches_expose_queries_in_stock_acp_fields(
    name: str, arguments: dict[str, Any], query: str
) -> None:
    start, end = project(name, arguments)
    assert start["kind"] == end["kind"] == "search"
    assert start["rawInput"]["query"] == end["rawInput"]["query"] == query


@pytest.mark.parametrize(
    ("name", "arguments", "title"),
    [
        (
            "process_read",
            {"sessions": [{"session_id": 12}, {"session_id": 34}]},
            "Monitoring processes · 12, 34",
        ),
        (
            "process_input",
            {"session_id": 12, "chars": "secret stdin"},
            "Send process input · 12",
        ),
        ("process_cancel", {"session_id": 12}, "Stop process · 12"),
        ("mcp_call", {"server": "github", "tool": "get_issue"}, "github.get_issue"),
        ("mcp_inspect", {"server": "github"}, "Inspect MCP tools · github"),
        ("skill", {"load": ["review", "testing"]}, "Load skills · review, testing"),
        ("custom_read_tool", {"path": "not-necessarily-a-file"}, "custom_read_tool"),
    ],
)
def test_other_tools_keep_useful_identity_not_result_text(
    name: str, arguments: dict[str, Any], title: str
) -> None:
    start, end = project(name, arguments, {"ok": True, "output": "{noisy result}"})
    assert start["title"] == end["title"] == title
    assert start["kind"] == end["kind"] == "other"


def test_presentation_never_mutates_native_arguments_or_results() -> None:
    arguments = {"cmd": "git status", "nested": {"value": [1, 2]}}
    result = {"ok": False, "output": "raw\n", "error": "bad", "nested": [1, 2]}
    originals = copy.deepcopy((arguments, result))
    _, end = project("command_exec", arguments, result, ok=False)
    assert (arguments, result) == originals
    assert end["rawOutput"] is result
    assert end["status"] == "failed"
    assert "Error: bad" in end["content"][0]["content"]["text"]


def test_executed_arguments_override_pre_hook_display_metadata() -> None:
    tools = ToolCallProjector()
    tools.update(
        start=True,
        data={
            "tool_call_id": "call",
            "tool_name": "read",
            "tool_arguments": '{"path":"/repo/before.py"}',
        },
    )
    end = tools.update(
        start=False,
        data={
            "tool_call_id": "call",
            "tool_name": "read",
            "executed_arguments": {"path": "after.py"},
            "ok": True,
        },
    )
    assert end["rawInput"]["path"] == "after.py"
    assert "after.py" in end["title"]
    assert end["locations"] == []


def test_display_aliases_do_not_overwrite_conflicting_invalid_input() -> None:
    arguments = {"cmd": "first", "command": "second"}
    start, end = project(
        "command_exec", arguments, {"error": "Conflicting fields"}, ok=False
    )
    assert start["rawInput"] == end["rawInput"] == arguments
    assert end["status"] == "failed"


def test_partial_failed_completion_retains_start_identity() -> None:
    tools = ToolCallProjector()
    start = tools.update(
        start=True,
        data={
            "tool_call_id": "call",
            "tool_name": "command_exec",
            "tool_arguments": "{broken json",
        },
    )
    end = tools.update(
        start=False,
        data={
            "tool_call_id": "call",
            "ok": False,
            "result": {"error": "Invalid arguments"},
        },
    )
    assert end["title"] == start["title"] == "Run command"
    assert end["rawInput"] == "{broken json"
    assert end["status"] == "failed"
    s.SessionNotification.model_validate({"sessionId": "session", "update": end})


def test_in_flight_metadata_is_isolated_and_released_on_completion() -> None:
    tools, other_turn = ToolCallProjector(), ToolCallProjector()
    for call_id, name in [("a", "first_tool"), ("b", "second_tool")]:
        tools.update(start=True, data={"tool_call_id": call_id, "tool_name": name})
    assert (
        other_turn.update(start=False, data={"tool_call_id": "a", "ok": True})["title"]
        == "Tool"
    )
    assert (
        tools.update(start=False, data={"tool_call_id": "b", "ok": True})["title"]
        == "second_tool"
    )
    assert (
        tools.update(start=False, data={"tool_call_id": "a", "ok": True})["title"]
        == "first_tool"
    )
    assert (
        tools.update(start=False, data={"tool_call_id": "a", "ok": True})["title"]
        == "Tool"
    )


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ("line one\nline two", "line one\nline two"),
        ({"content": "line one\nline two"}, "line one\nline two"),
        ({"stdout": "out", "stderr": "err"}, "out\n\nerr"),
        ({"result": {"content": [{"type": "text", "text": "MCP text"}]}}, "MCP text"),
        (
            {"output": [{"path": "src/app.py", "line": 7, "text": "match"}]},
            "src/app.py:7: match",
        ),
        ({"output": ["a.py", "b.py"]}, "a.py\nb.py"),
        ({"answer": 42}, '{\n  "answer": 42\n}'),
        ({"output": "\x1b[32mgreen\x1b[0m"}, "green"),
    ],
)
def test_readable_output(result: Any, expected: str) -> None:
    assert output_content(result)[0]["content"]["text"] == expected


def test_process_results_distinguish_launch_from_exit() -> None:
    _, running = project(
        "command_exec",
        {"cmd": "sleep 5"},
        {"ok": True, "session_id": 12, "status": "running", "exit_code": None},
    )
    assert running["status"] == "completed"  # The launch call, not the process.
    assert running["content"][0]["content"]["text"] == "Process 12 · running"
    result = {
        "items": [
            {
                "session_id": 12,
                "status": "failed",
                "exit_code": 1,
                "timed_out": True,
                "output": "failure",
            }
        ]
    }
    text = output_content(result)[0]["content"]["text"]
    assert text == "Process 12 · failed · exit 1 · timed out\n\nfailure"


def test_large_unicode_output_keeps_both_ends_below_client_tail_limit() -> None:
    output = "FIRST\n" + "🛠️ line\n" * 5000 + "\nLAST"
    result = {"ok": True, "output": output}
    _, end = project("command_exec", {"cmd": "tests"}, result)
    text = end["content"][0]["content"]["text"]
    assert len(text.encode("utf-8")) <= DISPLAY_BYTES
    assert len(text.encode("utf-16-le")) // 2 < 8000
    assert text.startswith("FIRST\n") and text.endswith("\nLAST")
    assert "[Display output truncated]" in text
    assert end["rawOutput"]["output"] == output


def test_long_lists_keep_the_last_entries_and_null_has_no_display() -> None:
    text = output_content([f"file-{i}" for i in range(100)])[0]["content"]["text"]
    assert text.startswith("file-0\n") and text.endswith("file-99")
    assert "[36 entries omitted]" in text
    assert output_content(None) == []
