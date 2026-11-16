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
        ["daemon", "status"],
        ["mcp", "demo"],
        ["--root", "/tmp/repo", "serve", "--port", "0"],
    ],
)
def test_prompt_injection_preserves_subcommands(argv: list[str]) -> None:
    assert _inject_prompt_flag(argv) == argv


def test_prompt_injection_still_converts_bare_prompt() -> None:
    assert _inject_prompt_flag(["fix the tests"]) == ["--prompt", "fix the tests"]


def test_resume_short_prompt_alias_uses_headless_entrypoint(monkeypatch):
    calls = []

    def resume(args, **kwargs):
        calls.append((args, kwargs))
        return 0

    monkeypatch.setattr("yoke.cli.backend.startup.run_daemon_headless", resume)
    result = CliRunner().invoke(app, ["resume", "saved", "-p", "again"])
    assert result.exit_code == 0, result.output
    args, options = calls[0]
    assert args.headless and args.prompt == "again"
    assert options["resume_id"] == "saved"


def test_acp_discovers_daemon_without_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("yoke.acp.runner.run_acp_with_daemon", lambda: 0)
    result = CliRunner().invoke(
        app, ["acp"], env={"YOKE_ACP_URL": "", "YOKE_ACP_TOKEN": ""}
    )

    assert result.exit_code == 0


def test_acp_explicit_url_requires_token() -> None:
    result = CliRunner().invoke(
        app, ["acp", "--url", "http://localhost:1234"], env={"YOKE_ACP_TOKEN": ""}
    )

    assert result.exit_code == 2
    assert "explicit ACP URL requires" in result.output


def test_acp_explicit_url_override(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = []
    monkeypatch.setattr(
        "yoke.acp.runner.run_acp", lambda *args: calls.append(args) or 0
    )
    result = CliRunner().invoke(
        app,
        ["acp"],
        env={"YOKE_ACP_URL": "http://localhost:1234", "YOKE_ACP_TOKEN": "secret"},
    )
    assert result.exit_code == 0
    assert calls == [("http://localhost:1234", "secret")]


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
