from __future__ import annotations

from pathlib import Path

from yoke.agent.prompting import render_active_skill_message
from yoke.agent.skills.discovery import load_skill
from yoke.agent.skills.registry import SkillRegistry


def test_active_skill_message_includes_skill_directory_files(
    tmp_path: Path,
) -> None:
    skill_dir = tmp_path / "demo-skill"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    reference_file = skill_dir / "reference.md"
    nested_file = skill_dir / "examples" / "workflow.py"
    nested_file.parent.mkdir()
    skill_file.write_text(
        "---\nname: demo-skill\ndescription: Demo skill.\n---\n\n# Demo\n",
        encoding="utf-8",
    )
    reference_file.write_text("reference", encoding="utf-8")
    nested_file.write_text("print('hi')", encoding="utf-8")

    spec = load_skill(skill_dir)
    active = SkillRegistry([spec]).activate("demo-skill")
    message = render_active_skill_message(active)

    assert isinstance(message.content, str)
    assert "files:" in message.content
    assert f"- {skill_file.resolve()}" in message.content
    assert f"- {reference_file.resolve()}" in message.content
    assert f"- {nested_file.resolve()}" in message.content
