"""CLI-facing loader error reporting tests for yoke."""

# ruff: noqa: S101

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from yoke.cli.config import CLIArgs
from yoke.cli.config import build_agent_from_args
from yoke.cli.main import app


def test_build_agent_reports_human_readable_skill_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Malformed skills bubble up as short, readable build errors."""
    home = tmp_path / "home"
    monkeypatch.setattr("yoke.cli.config.runtime.Path.home", lambda: home)
    monkeypatch.setattr("yoke.cli.bootstrap.config.Path.home", lambda: home)
    broken_skill_dir = home / ".yoke" / "skills" / "broken-skill"
    broken_skill_dir.mkdir(parents=True)
    (broken_skill_dir / "SKILL.md").write_text(
        "# missing frontmatter\n",
        encoding="utf-8",
    )
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True)
    (codex_dir / "auth.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError) as exc_info:
        build_agent_from_args(CLIArgs(root=str(tmp_path)))

    message = str(exc_info.value)
    assert (
        f"Skill file `{broken_skill_dir / 'SKILL.md'}` "
        "is missing YAML frontmatter." in message
    )
    assert "name:" in message
    assert "description:" in message


def test_tools_list_reports_human_readable_invalid_config_error(
    tmp_path: Path,
) -> None:
    """`tools list` reports invalid config JSON without a traceback."""
    config_dir = tmp_path / ".yoke"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        """{
  "tools": {
    "read": "allow",
    / "grep": "allow"
  }
}
""",
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["tools", "list", "--root", str(tmp_path)])

    assert result.exit_code == 1
    assert "Tool loading failed: Invalid yoke config file" in result.stdout
    assert "Invalid JSON:" in result.stdout


def test_tools_activate_reports_human_readable_invalid_existing_config(
    tmp_path: Path,
) -> None:
    """`tools activate` explains that a broken config must be fixed first."""
    config_dir = tmp_path / ".yoke"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        """{
  "tools": {
    "read": "allow",
    / "grep": "allow"
  }
}
""",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["tools", "activate", "edit", "--root", str(tmp_path)],
    )

    assert result.exit_code == 1
    assert "Could not update tool policy because" in result.stderr
    assert "Fix or remove that file first." in result.stderr


def test_skills_list_reports_human_readable_invalid_skill_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`skills list` reports malformed skills in plain language."""
    home = tmp_path / "home"
    monkeypatch.setattr("yoke.cli.config.runtime.Path.home", lambda: home)
    monkeypatch.setattr("yoke.cli.skills_app.Path.home", lambda: home)
    broken_skill_dir = home / ".yoke" / "skills" / "broken-skill"
    broken_skill_dir.mkdir(parents=True)
    (broken_skill_dir / "SKILL.md").write_text(
        "# missing frontmatter\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["skills", "list", "--root", str(tmp_path)])

    assert result.exit_code == 1
    assert "Skill loading failed:" in result.stderr
    assert "missing YAML frontmatter" in result.stderr


def test_skills_show_reports_unknown_skill_with_available_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`skills show` includes the requested name and available alternatives."""
    home = tmp_path / "home"
    monkeypatch.setattr("yoke.cli.config.runtime.Path.home", lambda: home)
    monkeypatch.setattr("yoke.cli.skills_app.Path.home", lambda: home)
    skill_dir = home / ".yoke" / "skills" / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: Demo skill.\n---\n\nUse me.\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["skills", "show", "missing-skill", "--root", str(tmp_path)],
    )

    assert result.exit_code == 1
    assert "Skill loading failed:" in result.stderr
    assert "Unknown skill `missing-skill`." in result.stderr
    assert "demo-skill" in result.stderr
