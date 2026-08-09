"""Tests for yoke skill discovery."""

from pathlib import Path

from yoke.agent.skills.discovery import load_skill


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
