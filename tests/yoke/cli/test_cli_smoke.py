"""Smoke tests for the yoke CLI command surface."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from typer.testing import CliRunner

from yoke.cli.main import app

CommandFactory = Callable[[Path], list[str]]


def _command(*parts: str) -> CommandFactory:
    return lambda _root: list(parts)


def _root_command(*parts: str) -> CommandFactory:
    return lambda root: [*parts, "--root", str(root)]


@pytest.mark.parametrize(
    "command_factory",
    [
        _command("--help"),
        _command("version"),
        _command("resume", "--help"),
        _command("continue", "--help"),
        _command("login", "--help"),
        _command("tools"),
        _command("tools", "--help"),
        _command("tools", "list"),
        _root_command("tools", "init"),
        _root_command("tools", "deactivate", "file.read"),
        _root_command("tools", "activate", "file.read"),
        _command("models"),
        _command("models", "--help"),
        _root_command("models", "list"),
        _root_command("models", "set", "codex:gpt-5.4-mini", "--repo"),
        _command("providers"),
        _command("providers", "--help"),
        _command("providers", "list"),
        _command("providers", "doctor"),
        _command("providers", "init", "smoke_provider"),
        _command("skills"),
        _command("skills", "--help"),
        _root_command("skills", "list"),
        _root_command("skills", "show", "create-skill"),
        _root_command("skills", "init", "smoke-skill"),
    ],
)
def test_cli_command_smoke(
    command_factory: CommandFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run non-interactive commands to catch import and dispatch regressions."""
    home = tmp_path / "home"
    root = tmp_path / "workspace"
    home.mkdir()
    root.mkdir()
    monkeypatch.setenv("HOME", str(home))

    runner = CliRunner()
    command = command_factory(root)
    result = runner.invoke(app, command, catch_exceptions=False)

    assert result.exit_code == 0, f"yoke {' '.join(command)} failed:\n{result.output}"
