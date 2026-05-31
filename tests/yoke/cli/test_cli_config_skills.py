from __future__ import annotations

# ruff: noqa: F403, F405
# ruff: noqa: ANN003, ANN202, D100, D103, F403, F405, S101

from .support import *  # noqa: F403, F405


@pytest.mark.parametrize(
    ("root_kind", "expected_count"),
    [("repo", 2), ("home", 1)],
)
def test_default_cli_skill_dirs_uses_existing_home_and_repo_dirs(
    tmp_path: Path, monkeypatch, root_kind: str, expected_count: int
) -> None:
    from yoke.cli.config import default_cli_skill_dirs

    home = tmp_path / "home"
    monkeypatch.setattr("yoke.cli.config.runtime.Path.home", lambda: home)
    (home / ".yoke" / "skills").mkdir(parents=True)
    (tmp_path / ".yoke" / "skills").mkdir(parents=True)
    root = tmp_path if root_kind == "repo" else home

    dirs = default_cli_skill_dirs(root)

    assert str((home / ".yoke" / "skills").resolve()) in dirs
    assert len(dirs) == expected_count


def test_skills_init_creates_scaffold(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    runner = CliRunner()
    result = runner.invoke(
        app,
        ["skills", "init", "demo-skill", "--root", str(tmp_path)],
    )

    assert result.exit_code == 0
    skill_file = tmp_path / ".yoke" / "skills" / "demo-skill" / "SKILL.md"
    assert skill_file.is_file()
    content = skill_file.read_text(encoding="utf-8")
    assert "name: demo-skill" in content
    assert "description:" in content


def test_skills_list_uses_default_cli_skill_dirs(tmp_path: Path, monkeypatch) -> None:
    from typer.testing import CliRunner

    home = tmp_path / "home"
    monkeypatch.setattr("yoke.cli.config.runtime.Path.home", lambda: home)
    monkeypatch.setattr("yoke.cli.skills_app.Path.home", lambda: home)
    home_skill = home / ".yoke" / "skills" / "home-skill"
    repo_skill = tmp_path / ".yoke" / "skills" / "repo-skill"
    home_skill.mkdir(parents=True)
    repo_skill.mkdir(parents=True)
    (home_skill / "SKILL.md").write_text(
        "---\nname: home-skill\ndescription: Home skill.\n---\n",
        encoding="utf-8",
    )
    (repo_skill / "SKILL.md").write_text(
        "---\nname: repo-skill\ndescription: Repo skill.\n---\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(app, ["skills", "list", "--root", str(tmp_path)])

    assert result.exit_code == 0
    assert "home-skill" in result.stdout
    assert "repo-skill" in result.stdout


def test_build_tool_report_ignores_skill_policy_when_skills_exist(
    tmp_path: Path, monkeypatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr("yoke.cli.config.runtime.Path.home", lambda: home)
    monkeypatch.setattr("yoke.cli.bootstrap.config.Path.home", lambda: home)
    (home / ".yoke" / "skills" / "demo-skill").mkdir(parents=True)
    ((home / ".yoke" / "skills" / "demo-skill") / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: Demo skill.\n---\n\nUse me.\n",
        encoding="utf-8",
    )
    config_dir = tmp_path / ".yoke"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        """
{
  "tools": {
    "skill": "deny"
  }
}
""".strip(),
        encoding="utf-8",
    )

    report = build_tool_report(root=tmp_path)

    active_names = {entry.tool.name for entry in report.active_tools}
    denied_names = {entry.tool.name for entry in report.denied_tools}
    assert "skill" not in active_names
    assert "skill" not in denied_names


def test_session_store_persists_skill_state(tmp_path: Path) -> None:
    from yoke.agent.skills.models import ActiveSkill
    from yoke.cli.session import SessionStore

    store = SessionStore(directory=tmp_path / "sessions")
    active_skill = ActiveSkill(
        name="demo-skill",
        description="Demo skill.",
        source_path=str(tmp_path / "skills" / "demo-skill" / "SKILL.md"),
        reload_on_next_use=False,
    )

    store.save(
        "session-1",
        [Message.user("hello")],
        active_skills=[active_skill],
        skill_dirs=[str(tmp_path / ".yoke" / "skills")],
        root=tmp_path,
    )
    record = store.load("session-1")

    assert record.active_skills[0].name == "demo-skill"
    assert record.skill_dirs == [str(tmp_path / ".yoke" / "skills")]
