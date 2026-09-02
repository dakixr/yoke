"""Tests for shell command compatibility rewrites."""

from __future__ import annotations

# ruff: noqa: S101

import os
import shutil
import subprocess
from pathlib import Path

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
    assert "$ErrorActionPreference = 'Continue'" in command[-1]
    assert "$ErrorActionPreference = $yokeErrorActionPreference" in command[-1]
    assert "NativeCommandError*" in command[-1]
    assert "$yokePowerShellError" in command[-1]
    assert "if ($yokeExitCode -ne 0) { exit $yokeExitCode }" in command[-1]


def test_powershell_chain_uses_native_exit_code_instead_of_error_stream() -> None:
    """Successful native stderr must not block the next legacy chain command."""
    command = rewrite_powershell_command("git fetch && git push")

    assert command.startswith("git fetch; $yokeSegmentSucceeded = $?;")
    assert "$LASTEXITCODE -eq 0" in command
    assert "NativeCommandError*" in command
    assert command.endswith("{ git push }")


def test_powershell_chain_nests_later_commands_under_prior_success() -> None:
    """Every later segment stays inside all preceding success guards."""
    command = rewrite_powershell_command("first && second && third")

    assert command.count("$yokeSegmentSucceeded = $?") == 2
    assert command.index("first") < command.index("second") < command.index("third")
    assert "{ second; $yokeSegmentSucceeded = $?; if " in command
    assert command.endswith("{ third } }")


@pytest.mark.skipif(os.name != "nt", reason="requires Windows PowerShell")
def test_windows_powershell_native_stderr_does_not_fail_successful_command(
    tmp_path: Path,
) -> None:
    """Windows PowerShell 5 judges a native process by its exit code."""
    shell_exe = shutil.which("powershell.exe") or shutil.which("powershell")
    if shell_exe is None:
        pytest.skip("Windows PowerShell is unavailable")
    env = os.environ.copy()
    env["VIRTUAL_ENV"] = str(tmp_path)
    command = build_powershell_command(
        "& $env:ComSpec /d /s /c 'echo harmless 1>&2 & exit /b 0'",
        env,
        shell_exe,
        Path(shell_exe).name.lower(),
    )

    completed = subprocess.run(  # noqa: S603
        command,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "harmless" in completed.stdout + completed.stderr


@pytest.mark.skipif(os.name != "nt", reason="requires Windows PowerShell")
def test_windows_powershell_still_propagates_real_exceptions(
    tmp_path: Path,
) -> None:
    """Relaxing native stderr handling must not swallow throw."""
    shell_exe = shutil.which("powershell.exe") or shutil.which("powershell")
    if shell_exe is None:
        pytest.skip("Windows PowerShell is unavailable")
    env = os.environ.copy()
    env["VIRTUAL_ENV"] = str(tmp_path)
    command = build_powershell_command(
        "throw 'real failure'",
        env,
        shell_exe,
        Path(shell_exe).name.lower(),
    )

    completed = subprocess.run(  # noqa: S603
        command,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "real failure" in completed.stdout + completed.stderr


@pytest.mark.skipif(os.name != "nt", reason="requires Windows PowerShell")
def test_windows_powershell_still_fails_nonterminating_errors(
    tmp_path: Path,
) -> None:
    """Native stderr filtering must not hide ordinary PowerShell errors."""
    shell_exe = shutil.which("powershell.exe") or shutil.which("powershell")
    if shell_exe is None:
        pytest.skip("Windows PowerShell is unavailable")
    env = os.environ.copy()
    env["VIRTUAL_ENV"] = str(tmp_path)
    command = build_powershell_command(
        "Write-Error 'real failure'",
        env,
        shell_exe,
        Path(shell_exe).name.lower(),
    )

    completed = subprocess.run(  # noqa: S603
        command,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode != 0
    assert "real failure" in completed.stdout + completed.stderr


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
