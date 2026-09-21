from __future__ import annotations

# ruff: noqa: D100,D103,S101

import pytest
from typer.testing import CliRunner

from yoke.cli.main import app
from yoke.cli.main_core import _inject_prompt_flag


@pytest.mark.parametrize(
    "argv",
    [
        ["serve", "--host", "127.0.0.1", "--port", "0"],
        ["acp"],
        ["mcp", "demo"],
        ["--root", "/tmp/repo", "serve", "--port", "0"],
    ],
)
def test_prompt_injection_preserves_subcommands(argv: list[str]) -> None:
    assert _inject_prompt_flag(argv) == argv


def test_prompt_injection_still_converts_bare_prompt() -> None:
    assert _inject_prompt_flag(["fix the tests"]) == ["--prompt", "fix the tests"]


def test_acp_requires_native_daemon_environment() -> None:
    result = CliRunner().invoke(
        app, ["acp"], env={"YOKE_ACP_URL": "", "YOKE_ACP_TOKEN": ""}
    )

    assert result.exit_code == 2
    assert "YOKE_ACP_URL and YOKE_ACP_TOKEN are required" in result.output


@pytest.mark.parametrize(
    "argv",
    [
        ["--help"],
        ["resume", "--help"],
        ["models", "set", "--help"],
    ],
)
def test_model_cli_uses_sdk_style_selection(argv: list[str]) -> None:
    result = CliRunner().invoke(app, argv)

    assert result.exit_code == 0
    assert "provider-name:model-name[:thinking-effort]" in result.output
    assert "--reasoning-effort" not in result.output
