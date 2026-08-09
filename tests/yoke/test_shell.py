"""Tests for shell command compatibility rewrites."""

from __future__ import annotations

# ruff: noqa: S101

import subprocess
import os

import pytest

from yoke.agent.tools.shell import (
    build_cmd_command,
    build_powershell_command,
    rewrite_powershell_command,
)
from tests.markers import skip_in_ci


def test_current_python_executable_prefers_active_virtualenv(
    monkeypatch,
    tmp_path,
) -> None:
    """Command tools use the venv active in the parent shell."""
    from yoke.agent.tools import python_env

    venv = tmp_path / ".venv"
    executable = venv / "Scripts" / "python.exe"
    executable.parent.mkdir(parents=True)
    executable.write_text("", encoding="utf-8")
    monkeypatch.setenv("VIRTUAL_ENV", str(venv))
    monkeypatch.delenv("CONDA_PREFIX", raising=False)
    monkeypatch.setattr(
        python_env,
        "python_executable_in_env",
        lambda _path: executable,
    )

    assert python_env.current_python_executable() == str(executable)


def test_build_powershell_command_propagates_native_exit_codes() -> None:
    """PowerShell bootstrap exits with nonzero native command status."""
    env: dict[str, str] = {}

    command = build_powershell_command(
        "python fail.py", env, "powershell", "powershell"
    )

    assert "$global:LASTEXITCODE = 0" in command[-1]
    assert "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }" in command[-1]


def test_build_cmd_command_preserves_quoted_payload() -> None:
    """Cmd payload quotes are not escaped by Python list2cmdline."""
    command = build_cmd_command("cmd.exe", 'start "" "C:\\path with spaces"')

    assert command == 'cmd.exe /d /s /c start "" "C:\\path with spaces"'


def test_build_cmd_command_wraps_leading_quoted_payload() -> None:
    """Leading-quoted cmd payloads get the outer quote pair required by /s."""
    command = build_cmd_command(
        "cmd.exe", '"C:\\path with spaces\\script.bat" "arg with spaces"'
    )

    assert command == (
        'cmd.exe /d /s /c ""C:\\path with spaces\\script.bat" "arg with spaces""'
    )


@skip_in_ci
@pytest.mark.skipif(os.name != "nt", reason="requires cmd.exe")
def test_build_cmd_command_runs_quoted_batch_path(tmp_path) -> None:
    """Quoted batch files under paths with spaces run through cmd wrapper."""
    bat_dir = tmp_path / "dir with spaces"
    bat_dir.mkdir()
    output_path = tmp_path / "output.txt"
    bat_path = bat_dir / "script with spaces.bat"
    bat_path.write_text(
        "@echo off\r\necho %~1>%2\r\n",
        encoding="utf-8",
    )
    command = build_cmd_command(
        "cmd.exe", f'"{bat_path}" "arg with spaces" "{output_path}"'
    )

    completed = subprocess.run(  # noqa: S603
        command,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert output_path.read_text(encoding="utf-8").strip() == "arg with spaces"


def test_rewrite_powershell_command_preserves_cmd_start_quotes() -> None:
    """Cmd /c start invocations keep cmd-style quoting under PowerShell."""
    command = rewrite_powershell_command('cmd /c start "" "C:\\path with spaces"')

    assert command == ('& $env:ComSpec /d /s /c \'start "" "C:\\path with spaces"\'')


def test_rewrite_powershell_command_preserves_quoted_batch_path() -> None:
    """Quoted batch paths remain quoted inside the cmd /c payload."""
    command = rewrite_powershell_command(
        'cmd /c "C:\\path with spaces\\script.bat" "arg with spaces"'
    )

    assert command == (
        "& $env:ComSpec /d /s /c "
        "'"
        '"C:\\path with spaces\\script.bat" "arg with spaces"'
        "'"
    )


def test_rewrite_powershell_command_keeps_cmd_chain_inside_cmd() -> None:
    """Cmd /c rewrites before PowerShell chain-operator compatibility."""
    command = rewrite_powershell_command("cmd /c echo hi && echo done")

    assert command == "& $env:ComSpec /d /s /c 'echo hi && echo done'"
