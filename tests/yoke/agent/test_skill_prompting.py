from __future__ import annotations

import shutil
from pathlib import Path

from yoke.agent.prompting import render_active_skill_message
from yoke.agent.skills.activation import activate_skills
from yoke.agent.skills.models import ActiveSkill
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


def test_active_skill_snapshot_survives_deleted_source(tmp_path: Path) -> None:
    skill_dir = tmp_path / "demo-skill"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        "---\nname: demo-skill\ndescription: Demo skill.\n---\n\nBe durable.\n",
        encoding="utf-8",
    )
    active = SkillRegistry([load_skill(skill_dir)]).activate("demo-skill")

    skill_file.write_text("broken current instructions", encoding="utf-8")

    assert active.load_content().endswith("Be durable.\n")

    shutil.rmtree(skill_dir)

    assert active.load_content().endswith("Be durable.\n")
    active.source_path = ""
    assert active.load_content().endswith("Be durable.\n")
    message = render_active_skill_message(active)
    assert isinstance(message.content, str)
    assert "Be durable." in message.content


def test_reconcile_active_skills_drops_unrecoverable_legacy_state(
    tmp_path: Path,
) -> None:
    legacy = ActiveSkill(
        name="removed-skill",
        description="Removed skill.",
        source_path=str(tmp_path / "removed-skill" / "SKILL.md"),
        reload_on_next_use=True,
    )

    reconciled = SkillRegistry([]).reconcile([legacy])

    assert reconciled == []


def test_reloading_active_skill_refreshes_moved_source(tmp_path: Path) -> None:
    old_source = tmp_path / "old" / "demo-skill" / "SKILL.md"
    old = ActiveSkill(
        name="demo-skill",
        description="Old description.",
        source_path=str(old_source),
        content="old instructions",
        reload_on_next_use=False,
    )
    new_dir = tmp_path / "new" / "demo-skill"
    new_dir.mkdir(parents=True)
    new_file = new_dir / "SKILL.md"
    new_file.write_text(
        "---\nname: demo-skill\ndescription: New description.\n---\n\nNew instructions.\n",
        encoding="utf-8",
    )
    registry = SkillRegistry([load_skill(new_dir)])

    reconciled = registry.reconcile([old])

    assert len(reconciled) == 1
    assert reconciled[0].description == "New description."
    assert reconciled[0].source_path == str(new_file.resolve())
    assert reconciled[0].content is not None
    assert "New instructions." in reconciled[0].content
    assert reconciled[0].reload_on_next_use is False


def test_reconcile_uses_explicit_available_skill_specs(tmp_path: Path) -> None:
    from yoke.agent.loop import MessageHistory
    from yoke.agent.loop import RuntimeAgent
    from yoke.ai.providers.base import Provider

    class NoopProvider(Provider):
        def complete(self, messages, tools):
            raise AssertionError("load_conversation must not call the provider")

    skill_dir = tmp_path / "demo-skill"
    skill_dir.mkdir()
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(
        "---\nname: demo-skill\ndescription: Current skill.\n---\n\nCurrent.\n",
        encoding="utf-8",
    )
    spec = load_skill(skill_dir)
    legacy = ActiveSkill(
        name="demo-skill",
        description="Legacy skill.",
        source_path=str(skill_file),
    )
    agent = RuntimeAgent(provider=NoopProvider(), tools=[], available_skills=[spec])

    agent.load_conversation(
        MessageHistory([]),
        available_skills=[spec],
        active_skills=[legacy],
    )

    assert len(agent.active_skills) == 1
    assert agent.active_skills[0].description == "Current skill."
    assert agent.active_skills[0].content is not None


def test_active_snapshot_can_reload_without_registry_source(tmp_path: Path) -> None:
    snapshot = ActiveSkill(
        name="removed-skill",
        description="Removed skill.",
        source_path=str(tmp_path / "removed-skill" / "SKILL.md"),
        content="Durable instructions.",
        reload_on_next_use=False,
    )

    result = activate_skills(
        registry=SkillRegistry([]),
        active_skills=[snapshot],
        names=["removed-skill"],
    )

    assert result.ok
    assert result.reloaded == ["removed-skill"]
    assert result.active_skills[0].reload_on_next_use is True
