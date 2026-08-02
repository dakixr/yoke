"""Tests for skill prompt rendering."""

import ast
import re
from pathlib import Path
from unittest import TestCase

from yoke.agent.context.manager import ContextManager
from yoke.agent.prompting import render_active_skill_message
from yoke.agent.skills.activation import activate_skills
from yoke.agent.skills.context import (
    append_missing_active_skill_messages,
)
from yoke.agent.skills.models import ActiveSkill
from yoke.agent.skills.models import SkillSpec
from yoke.agent.skills.registry import SkillRegistry


def test_active_skill_message_lists_skill_directory_files(
    tmp_path: Path,
) -> None:
    """Active skill prompt includes full paths in the skill directory."""
    skill_root = tmp_path / "example-skill"
    nested_root = skill_root / "reference"
    nested_root.mkdir(parents=True)
    skill_md = skill_root / "SKILL.md"
    skill_md.write_text("skill instructions", encoding="utf-8")
    reference = nested_root / "guide.md"
    reference.write_text("guide", encoding="utf-8")

    message = render_active_skill_message(
        ActiveSkill(
            name="example-skill",
            description="Example skill.",
            source_path=str(skill_md),
        )
    )

    content = message.plain_text_content or ""
    test_case = TestCase()
    test_case.assertIn("Skill directory files:", content)
    test_case.assertIn(f"- {skill_md}", content)
    test_case.assertIn(f"- {reference}", content)
    test_case.assertIn("skill instructions", content)


def test_active_skill_message_uses_cached_content_when_file_is_gone(
    tmp_path: Path,
) -> None:
    """Active skill prompt survives a deleted original SKILL.md file."""
    skill_root = tmp_path / "deleted-skill"
    skill_root.mkdir()
    skill_md = skill_root / "SKILL.md"

    message = render_active_skill_message(
        ActiveSkill(
            name="deleted-skill",
            description="Deleted skill.",
            source_path=str(skill_md),
            content="cached skill instructions",
        )
    )

    content = message.plain_text_content or ""
    test_case = TestCase()
    test_case.assertIn("cached skill instructions", content)


def test_active_skill_message_does_not_crash_without_cached_content(
    tmp_path: Path,
) -> None:
    """Legacy active skills without cached content render an explanation."""
    skill_md = tmp_path / "missing-skill" / "SKILL.md"

    message = render_active_skill_message(
        ActiveSkill(
            name="missing-skill",
            description="Missing skill.",
            source_path=str(skill_md),
            content=None,
        )
    )

    content = message.plain_text_content or ""
    test_case = TestCase()
    test_case.assertIn("Skill content unavailable for `missing-skill`", content)


def test_skill_activation_does_not_crash_when_file_disappears(
    tmp_path: Path,
) -> None:
    """Activation stores a non-blocking unavailable message."""
    skill_root = tmp_path / "vanished-skill"
    skill_md = skill_root / "SKILL.md"
    skill_root.mkdir()
    spec = SkillSpec(
        name="vanished-skill",
        description="Vanished skill.",
        root=skill_root,
        skill_md_path=skill_md,
    )

    active_skill = SkillRegistry([spec]).activate("vanished-skill")

    test_case = TestCase()
    test_case.assertIsNotNone(active_skill.content)
    test_case.assertIn("Skill content unavailable", active_skill.content or "")


def test_tool_skill_message_keeps_activation_identity(tmp_path: Path) -> None:
    """A tool-rendered skill event is not duplicated on the next turn."""
    skill_root = tmp_path / "tool-skill"
    skill_root.mkdir()
    skill_md = skill_root / "SKILL.md"
    skill_md.write_text("tool skill instructions", encoding="utf-8")
    registry = SkillRegistry(
        [
            SkillSpec(
                name="tool-skill",
                description="Tool skill.",
                root=skill_root,
                skill_md_path=skill_md,
            )
        ]
    )
    activation = activate_skills(
        registry=registry,
        active_skills=[],
        names=["tool-skill"],
    )
    manager = ContextManager()
    context = manager.initialize(
        "",
        append_prompt=False,
        active_skills=activation.active_skills,
    )

    manager.append_skill_message(
        context,
        render_active_skill_message(registry.activate("tool-skill")),
    )
    append_missing_active_skill_messages(context)

    skill_events = [
        entry
        for entry in context.conversation_log.entries
        if entry.kind == "skill_event"
    ]
    assert len(skill_events) == 1
    assert (
        skill_events[0].metadata.get("skill_activation_id")
        == activation.active_skills[0].activation_id
    )


def test_builtin_skills_include_yoke_subagents_references() -> None:
    from yoke.agent.skills.discovery import builtin_skill_dir
    from yoke.agent.skills.discovery import discover_skills

    skills = {skill.name: skill for skill in discover_skills([])}

    assert set(skills) == {"create-skill", "yoke-subagents"}
    subagents = skills["yoke-subagents"]
    assert {
        path.relative_to(subagents.root).as_posix()
        for path in subagents.root.rglob("*")
        if path.is_file()
    } == {
        "CAPABILITIES.md",
        "COMMON.md",
        "PATTERNS.md",
        "SDK_SURFACE.md",
        "SKILL.md",
        "patterns/audit-research.md",
        "patterns/coder-reviewer.md",
        "patterns/pipeline.md",
        "patterns/review-merge.md",
    }
    assert subagents.root.parent == builtin_skill_dir().resolve()


def test_yoke_subagents_python_examples_compile() -> None:
    from yoke.agent.skills.discovery import builtin_skill_dir

    root = builtin_skill_dir() / "yoke-subagents"
    blocks = []
    for path in root.rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        blocks.extend(
            (path, source)
            for source in re.findall(r"```python\n(.*?)```", text, re.DOTALL)
        )

    assert blocks
    for path, source in blocks:
        compile(source, str(path), "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)


def test_yoke_subagents_patterns_keep_safety_guards_discoverable() -> None:
    from yoke.agent.skills.discovery import builtin_skill_dir

    root = builtin_skill_dir() / "yoke-subagents"
    skill = (root / "SKILL.md").read_text(encoding="utf-8")
    common = (root / "COMMON.md").read_text(encoding="utf-8")
    capabilities = (root / "CAPABILITIES.md").read_text(encoding="utf-8")
    pipeline = (root / "patterns" / "pipeline.md").read_text(encoding="utf-8")
    pair = (root / "patterns" / "coder-reviewer.md").read_text(encoding="utf-8")

    assert "Progressive references" in skill
    assert "discover_capabilities" in capabilities
    assert "require_batch_integrity" in common
    assert "preflight_selections" in pipeline
    assert "mutation_attempts: int = 1" in pipeline
    assert "AsyncExitStack" in pair
    assert "task_context" in pair
    assert "checked_files" in pair
    assert "not review.checked_files or not review.evidence" in pair
    assert "result.require_completed()" in skill


def test_yoke_subagents_markdown_links_resolve() -> None:
    from yoke.agent.skills.discovery import builtin_skill_dir

    root = builtin_skill_dir() / "yoke-subagents"
    for path in root.rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        for target in re.findall(r"\[[^]]+\]\(([^)]+)\)", text):
            relative = target.split("#", 1)[0]
            if not relative or "://" in relative:
                continue
            assert (path.parent / relative).resolve().exists(), (
                f"Broken link {target!r} in {path}"
            )
