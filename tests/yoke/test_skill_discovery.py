"""Tests for yoke skill discovery."""

from pathlib import Path

from yoke.agent.skills.discovery import builtin_skill_dir
from yoke.agent.skills.discovery import discover_skills
from yoke.agent.skills.discovery import load_skill
from yoke.agent.skills.paths import default_skill_dirs


def _write_skill(parent: Path, name: str, description: str) -> Path:
    skill_root = parent / name
    skill_root.mkdir(parents=True)
    (skill_root / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        "---\n\n"
        "Follow the instructions.\n",
        encoding="utf-8",
    )
    return skill_root


def test_load_skill_accepts_utf8_bom(tmp_path: Path) -> None:
    """A UTF-8 BOM before frontmatter does not invalidate a skill."""
    skill_root = tmp_path / "bom-skill"
    skill_root.mkdir()
    (skill_root / "SKILL.md").write_text(
        "---\n"
        "name: bom-skill\n"
        "description: A BOM-prefixed skill.\n"
        "---\n\n"
        "Follow the instructions.\n",
        encoding="utf-8-sig",
    )

    skill = load_skill(skill_root)

    assert skill.name == "bom-skill"
    assert skill.description == "A BOM-prefixed skill."


def test_builtin_yoke_sessions_replaces_resume_skill() -> None:
    names = {skill.name for skill in discover_skills([])}

    assert "yoke-sessions" in names
    assert "yoke-session-resume" not in names


def test_builtin_skill_wins_over_duplicate_configured_skill(tmp_path: Path) -> None:
    duplicate = tmp_path / "yoke-subagents"
    duplicate.mkdir()
    (duplicate / "SKILL.md").write_text("not valid frontmatter\n", encoding="utf-8")

    skill = next(
        skill for skill in discover_skills([tmp_path]) if skill.name == "yoke-subagents"
    )

    assert skill.root == builtin_skill_dir() / "yoke-subagents"


def test_repo_skill_wins_over_duplicate_global_skill(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    home = tmp_path / "home"
    repo_skills = root / ".yoke" / "skills"
    global_skills = home / ".yoke" / "skills"
    _write_skill(repo_skills, "shared-skill", "Repo skill.")
    _write_skill(global_skills, "shared-skill", "Global skill.")

    skill_dirs = default_skill_dirs(root, home=home)
    skill = next(
        skill
        for skill in discover_skills([Path(path) for path in skill_dirs])
        if skill.name == "shared-skill"
    )

    assert skill_dirs == [str(repo_skills.resolve()), str(global_skills.resolve())]
    assert skill.description == "Repo skill."


def test_default_skill_dirs_deduplicates_home_root(tmp_path: Path) -> None:
    skills = tmp_path / ".yoke" / "skills"
    skills.mkdir(parents=True)

    assert default_skill_dirs(tmp_path, home=tmp_path) == [str(skills.resolve())]
