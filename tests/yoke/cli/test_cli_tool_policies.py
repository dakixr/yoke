from __future__ import annotations

# ruff: noqa: F403, F405
# ruff: noqa: D100, D103, F403, F405, S101

import re
import sys

from .support import *  # noqa: F403, F405


def test_cli_prints_tool_discovery_message_in_interactive_mode(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    tools_dir = tmp_path / ".yoke"
    tools_dir.mkdir(parents=True)
    (tools_dir / "repo_echo.py").write_text(
        """
from yoke.agent.tools import LocalTool


class RepoEchoTool(LocalTool):
    name = "repo_echo"
    description = "repo"

    def execute(self) -> dict[str, object]:
        return {"ok": True}


def register_tools(context):
    return [RepoEchoTool.bind()]
""".strip(),
        encoding="utf-8",
    )
    agent = FakeAgent()
    agent.tool_report = build_tool_report(root=tmp_path)
    exit_code = run_cli(
        CLIArgs(root=str(tmp_path), prompt=None),
        agent=agent,
        input_func=lambda _="": "quit",
    )
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "repo tools from .yoke" in out


def test_cli_omits_provider_and_model_interactive_scrollback_note() -> None:
    stdout = CaptureStream()
    stderr = CaptureStream()
    agent = FakeAgent()
    agent.provider = FakeProvider()  # pyright: ignore[reportAttributeAccessIssue]

    exit_code = run_cli(
        CLIArgs(prompt=None),
        agent=agent,
        input_func=lambda _="": "quit",
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert "note Using provider FakeProvider with model gpt-test" not in (
        stdout.getvalue()
    )


def test_tools_init_creates_scaffold(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    runner = CliRunner()
    result = runner.invoke(app, ["tools", "init", "--root", str(tmp_path)])

    assert result.exit_code == 0
    created = tmp_path / ".yoke" / "example_tools.py"
    assert created.exists()
    text = created.read_text(encoding="utf-8")
    assert "@function_tool" in text
    assert "@class_tool" in text


def test_tools_list_shows_loaded_tools_and_success_message(
    tmp_path: Path,
) -> None:
    from typer.testing import CliRunner

    tools_dir = tmp_path / ".yoke"
    tools_dir.mkdir(parents=True)
    (tools_dir / "repo_echo.py").write_text(
        """
from yoke.agent.tools import LocalTool


class RepoEchoTool(LocalTool):
    name = "repo_echo"
    description = "repo"

    def execute(self) -> dict[str, object]:
        return {"ok": True}


def register_tools(context):
    return [RepoEchoTool.bind()]
""".strip(),
        encoding="utf-8",
    )
    runner = CliRunner()
    list_result = runner.invoke(app, ["tools", "list", "--root", str(tmp_path)])

    assert list_result.exit_code == 0
    assert "Tool loading OK." in list_result.stdout
    assert "Tool Inventory" in list_result.stdout
    assert "repo_echo" in list_result.stdout
    assert "repo" in list_result.stdout
    assert "extract_file_context" in list_result.stdout


def test_tools_list_returns_non_zero_on_conflict(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    tools_dir = tmp_path / ".yoke"
    tools_dir.mkdir(parents=True)
    (tools_dir / "one.py").write_text(
        """
from yoke.cli.tools.decorators import function_tool

@function_tool(name='dup')
def a(text: str) -> dict[str, object]:
    return {'ok': True, 'text': text}
""".strip(),
        encoding="utf-8",
    )
    (tools_dir / "two.py").write_text(
        """
from yoke.cli.tools.decorators import function_tool

@function_tool(name='dup')
def b(text: str) -> dict[str, object]:
    return {'ok': True, 'text': text}
""".strip(),
        encoding="utf-8",
    )

    runner = CliRunner()
    list_result = runner.invoke(app, ["tools", "list", "--root", str(tmp_path)])

    assert list_result.exit_code == 1
    assert "Tool loading failed:" in list_result.stdout


def test_tools_list_reports_unmatched_config_patterns(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    config_dir = tmp_path / ".yoke"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        """
{
  "tools": {
    "missing_*": "deny",
    "edit": "deny"
  }
}
""".strip(),
        encoding="utf-8",
    )

    runner = CliRunner()
    list_result = runner.invoke(app, ["tools", "list", "--root", str(tmp_path)])

    assert list_result.exit_code == 0
    assert "Tool Inventory" in list_result.stdout
    assert "edit" in list_result.stdout
    assert (
        "Warning: tool rule did not match any loaded tool: missing_*"
        in list_result.stdout
    )


def test_tools_list_shows_document_and_web_tools_as_builtins(
    tmp_path: Path,
) -> None:
    from typer.testing import CliRunner

    runner = CliRunner()
    list_result = runner.invoke(app, ["tools", "list", "--root", str(tmp_path)])

    assert list_result.exit_code == 0
    assert "extract_file_context" in list_result.stdout
    assert "web_fetch" in list_result.stdout
    assert "web_research" in list_result.stdout
    assert "default" in list_result.stdout
    assert "active" in list_result.stdout


def test_print_tool_inventory_table_colors_active_and_disabled_statuses(
    tmp_path: Path,
) -> None:
    from yoke.cli.tools.app import print_tool_inventory_table

    config_dir = tmp_path / ".yoke"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps({"tools": {"edit": "deny"}}),
        encoding="utf-8",
    )

    report = build_tool_report(root=tmp_path)
    stream = EncodedTTYCaptureStream()

    print_tool_inventory_table(stream, report)

    output = stream.getvalue()
    assert re.search(r"\x1b\[[0-9;]*32m\s*active\s*\x1b\[0m", output)
    assert re.search(r"\x1b\[[0-9;]*31m\s*disabled\s*\x1b\[0m", output)


def test_tools_list_colors_success_warning_and_failure_messages(
    tmp_path: Path,
) -> None:
    from yoke.cli.tools.app import tools_list

    config_dir = tmp_path / ".yoke"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps({"tools": {"missing_*": "deny"}}),
        encoding="utf-8",
    )

    stdout = EncodedTTYCaptureStream()
    stderr = EncodedTTYCaptureStream()
    old_stdout, old_stderr = sys.stdout, sys.stderr
    try:
        sys.stdout = stdout
        sys.stderr = stderr
        tools_list(root=tmp_path)
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr

    output = stdout.getvalue()
    assert re.search(r"\x1b\[[0-9;]*32mTool loading OK\.\x1b\[0m", output)
    assert re.search(
        r"\x1b\[[0-9;]*33mWarning: tool rule did not match any loaded "
        r"tool: missing_\*\x1b\[0m",
        output,
    )

    broken_dir = tmp_path / ".yoke"
    (broken_dir / "broken.py").write_text(
        "raise RuntimeError('boom')\n",
        encoding="utf-8",
    )

    stdout = EncodedTTYCaptureStream()
    stderr = EncodedTTYCaptureStream()
    old_stdout, old_stderr = sys.stdout, sys.stderr
    try:
        sys.stdout = stdout
        sys.stderr = stderr
        tools_list(root=tmp_path)
    finally:
        sys.stdout = old_stdout
        sys.stderr = old_stderr

    failure_output = stdout.getvalue()
    assert re.search(r"\x1b\[[0-9;]*32mTool loading OK\.\x1b\[0m", failure_output)


def test_tools_activate_writes_repo_policy(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["tools", "activate", "extract_file_context", "--root", str(tmp_path)],
    )

    assert result.exit_code == 0
    config_path = tmp_path / ".yoke" / "config.json"
    assert json.loads(config_path.read_text(encoding="utf-8")) == {
        "tools": {"extract_file_context": "allow"}
    }

    report = build_tool_report(root=tmp_path)
    names = {entry.tool.name for entry in report.active_tools}
    assert "extract_file_context" in names


def test_tools_deactivate_writes_repo_policy(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["tools", "deactivate", "edit", "--repo", "--root", str(tmp_path)],
    )

    assert result.exit_code == 0
    config_path = tmp_path / ".yoke" / "config.json"
    assert json.loads(config_path.read_text(encoding="utf-8")) == {
        "tools": {"edit": "deny"}
    }

    report = build_tool_report(root=tmp_path)
    names = {entry.tool.name for entry in report.active_tools}
    assert "edit" not in names


def test_tools_activate_writes_global_policy(tmp_path: Path, monkeypatch) -> None:
    from typer.testing import CliRunner

    home = tmp_path / "home"
    monkeypatch.setattr("yoke.cli.tools.app.Path.home", lambda: home)
    monkeypatch.setattr("yoke.cli.bootstrap.config.Path.home", lambda: home)

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "tools",
            "activate",
            "extract_file_context",
            "--global",
            "--root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    config_path = home / ".yoke" / "config.json"
    assert json.loads(config_path.read_text(encoding="utf-8")) == {
        "tools": {"extract_file_context": "allow"}
    }

    report = build_tool_report(root=tmp_path)
    names = {entry.tool.name for entry in report.active_tools}
    assert "extract_file_context" in names


def test_tools_policy_scope_flags_are_mutually_exclusive(
    tmp_path: Path,
) -> None:
    from typer.testing import CliRunner

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "tools",
            "activate",
            "read",
            "--repo",
            "--global",
            "--root",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 2
    assert "Use either --global or --repo, not both." in result.stderr


def test_cli_agent_keeps_document_tool_enabled_from_config(
    tmp_path: Path,
) -> None:
    config_dir = tmp_path / ".yoke"
    config_dir.mkdir(parents=True)
    (tmp_path / "notes.txt").write_text("alpha\nbeta\n", encoding="utf-8")
    (config_dir / "config.json").write_text(
        """
{
  "tools": {
    "extract_file_context": "allow"
  }
}
""".strip(),
        encoding="utf-8",
    )

    report = build_tool_report(root=tmp_path)
    names = {entry.tool.name for entry in report.active_tools}

    assert "extract_file_context" in names
