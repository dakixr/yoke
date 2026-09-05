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
        tool.parse_arguments({"raw_args": "needle"}).execute(),
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
        assert "regex parse error" in result["output"]


def test_rg_error_without_process_output_has_diagnostic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result = _execute_rg(
        tmp_path,
        monkeypatch,
        subprocess.CompletedProcess(args=["rg"], returncode=2, stdout="", stderr=""),
    )

    assert result == {
        "ok": False,
        "output": "rg failed with exit code 2",
        "exit_code": 2,
    }


@pytest.mark.parametrize("value", [0, 200_001])
def test_rg_rejects_invalid_max_output_chars(value: int) -> None:
    with pytest.raises(ValidationError):
        RipgrepTool.model_validate({"raw_args": "needle", "max_output_chars": value})
