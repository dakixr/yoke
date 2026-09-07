from __future__ import annotations

# ruff: noqa: D100,D103,S101

from pathlib import Path

from yoke.cli.skills_app import skills_show


def test_skills_show_lists_skill_directory_files_before_instructions(
    tmp_path: Path,
    capsys,
) -> None:
    skill_root = tmp_path / ".yoke" / "skills" / "review"
    reference_root = skill_root / "reference"
    reference_root.mkdir(parents=True)
    skill_path = skill_root / "SKILL.md"
    skill_path.write_text(
        "---\nname: review\ndescription: Review changes.\n---\n\n# Review\n",
        encoding="utf-8",
    )
    guide_path = reference_root / "guide.md"
    guide_path.write_text("# Guide\n", encoding="utf-8")

    skills_show("review", root=tmp_path)

    output = capsys.readouterr().out
    assert "Skill directory files:" in output
    assert f"- {skill_path.resolve()}" in output
    assert f"- {guide_path.resolve()}" in output
    assert output.index(str(skill_path.resolve())) < output.index("# Review")
