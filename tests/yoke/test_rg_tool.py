from __future__ import annotations

# ruff: noqa: D100, D103, S101

import json
import subprocess
from pathlib import Path
from typing import Any
from typing import cast

import pytest
from pydantic import ValidationError

from yoke.agent.tools import RipgrepTool
import yoke.agent.tools.rg as rg_module


def _execute_rg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    completed: subprocess.CompletedProcess[str],
) -> dict[str, Any]:
    monkeypatch.setattr(rg_module, "_resolve_rg_binary", lambda: "/test-bin/rg")
    monkeypatch.setattr(rg_module.subprocess, "run", lambda *args, **kwargs: completed)
    tool = RipgrepTool.bind(root=tmp_path)
    return cast(
        dict[str, Any],
        tool.parse_arguments({"patterns": ["needle"]}).execute(),
    )


@pytest.mark.parametrize(
    ("completed", "expected_ok"),
    [
        (
            subprocess.CompletedProcess(
                args=["rg"],
                returncode=0,
                stdout=json.dumps(
                    {
                        "type": "match",
                        "data": {
                            "path": {"text": "one.txt"},
                            "lines": {"text": "needle\n"},
                            "line_number": 1,
                        },
                    }
                ),
                stderr="",
            ),
            True,
        ),
        (
            subprocess.CompletedProcess(
                args=["rg"], returncode=1, stdout="", stderr=""
            ),
            True,
        ),
        (
            subprocess.CompletedProcess(
                args=["rg"],
                returncode=2,
                stdout="",
                stderr="regex parse error",
            ),
            False,
        ),
    ],
)
def test_rg_exit_status_controls_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    completed: subprocess.CompletedProcess[str],
    expected_ok: bool,
) -> None:
    result = _execute_rg(tmp_path, monkeypatch, completed)

    assert result["ok"] is expected_ok
    if completed.returncode == 2:
        assert result["exit_code"] == 2
        assert "regex parse error" in result["error"]


def test_rg_error_without_process_output_has_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _execute_rg(
        tmp_path,
        monkeypatch,
        subprocess.CompletedProcess(args=["rg"], returncode=2, stdout="", stderr=""),
    )

    assert result["ok"] is False
    assert result["error"] == "rg failed with exit code 2"
    assert result["exit_code"] == 2


@pytest.mark.parametrize("value", [0, 200_001])
def test_rg_rejects_invalid_max_output_chars(value: int) -> None:
    with pytest.raises(ValidationError):
        RipgrepTool.model_validate({"patterns": ["needle"], "max_output_chars": value})


def test_rg_rejects_raw_args_contract() -> None:
    with pytest.raises(ValidationError):
        RipgrepTool.model_validate({"raw_args": "needle | head -20"})


def test_rg_typed_command_uses_structured_options(tmp_path: Path) -> None:
    tool = cast(
        RipgrepTool,
        RipgrepTool.bind(root=tmp_path).parse_arguments(
            {
                "patterns": ["needle", "other"],
                "paths": ["src", "tests"],
                "globs": ["*.py", "!vendor/**"],
                "types": ["py"],
                "case": "insensitive",
                "context_before": 2,
                "context_after": 3,
                "limit": 20,
            }
        ),
    )

    command = tool._build_command("rg", tmp_path)

    assert command[:2] == ["rg", "--json"]
    assert command.count("--regexp") == 2
    assert command.count("--glob") == 2
    assert ["--before-context", "2"] == command[
        command.index("--before-context") : command.index("--before-context") + 2
    ]
    assert ["--after-context", "3"] == command[
        command.index("--after-context") : command.index("--after-context") + 2
    ]
    assert command[-3:] == ["--", "src", "tests"]
    assert "|" not in command


def test_rg_count_mode_parses_typed_counts(tmp_path: Path) -> None:
    tool = cast(
        RipgrepTool,
        RipgrepTool.bind(root=tmp_path).parse_arguments(
            {"patterns": ["needle"], "mode": "count"}
        ),
    )

    result = tool._render_output(
        "one.py\x002\ntwo.py\x001\n",
        "",
        ["rg"],
        0,
    )

    assert result["ok"] is True
    assert result["output"] == [
        {"path": "one.py", "count": 2},
        {"path": "two.py", "count": 1},
    ]


def test_rg_json_parser_ignores_non_object_json(tmp_path: Path) -> None:
    tool = cast(
        RipgrepTool,
        RipgrepTool.bind(root=tmp_path).parse_arguments({"patterns": ["needle"]}),
    )

    result = tool._render_output("2\n", "", ["rg"], 0)

    assert result["ok"] is True
    assert result["output"] == []


def test_rg_context_events_are_returned(tmp_path: Path) -> None:
    tool = cast(
        RipgrepTool,
        RipgrepTool.bind(root=tmp_path).parse_arguments(
            {"patterns": ["needle"], "context_before": 1, "context_after": 1}
        ),
    )
    stdout = "\n".join(
        json.dumps(event)
        for event in (
            {
                "type": "context",
                "data": {
                    "path": {"text": "one.txt"},
                    "lines": {"text": "before\n"},
                    "line_number": 1,
                    "submatches": [],
                },
            },
            {
                "type": "match",
                "data": {
                    "path": {"text": "one.txt"},
                    "lines": {"text": "needle\n"},
                    "line_number": 2,
                    "submatches": [{"match": {"text": "needle"}, "start": 0, "end": 6}],
                },
            },
        )
    )

    result = tool._render_output(stdout, "", ["rg"], 0)

    output = cast(list[dict[str, object]], result["output"])
    assert [item["kind"] for item in output] == ["context", "match"]


def test_rg_nul_parsers_preserve_newlines_in_paths(tmp_path: Path) -> None:
    files_tool = cast(
        RipgrepTool,
        RipgrepTool.bind(root=tmp_path).parse_arguments({"mode": "files"}),
    )
    count_tool = cast(
        RipgrepTool,
        RipgrepTool.bind(root=tmp_path).parse_arguments(
            {"patterns": ["needle"], "mode": "count"}
        ),
    )

    files_result = files_tool._render_output("plain\x00line\nname\x00", "", ["rg"], 0)
    count_result = count_tool._render_output("line\nname\x002\n", "", ["rg"], 0)

    assert files_result["output"] == ["plain", "line\nname"]
    assert count_result["output"] == [{"path": "line\nname", "count": 2}]


def test_rg_bounds_failure_diagnostics(tmp_path: Path) -> None:
    tool = cast(
        RipgrepTool,
        RipgrepTool.bind(root=tmp_path).parse_arguments(
            {"patterns": ["needle"], "max_output_chars": 20}
        ),
    )

    result = tool._render_output("", "x" * 200, ["rg"], 2)

    assert result["ok"] is False
    assert len(cast(str, result["error"])) == 20
    assert result["truncated"] is True
