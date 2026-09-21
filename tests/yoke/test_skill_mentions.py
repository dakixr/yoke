"""Tests for explicit skill mentions in user prompts."""

from pathlib import Path

from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import Message
from yoke.agent.skills.mentions import extract_skill_mentions
from yoke.agent.skills.models import SkillSpec
from yoke.agent.skills.registry import SkillRegistry
from yoke.ai.providers.base import Provider


class _CaptureProvider(Provider):
    def __init__(self) -> None:
        self.requests: list[list[Message]] = []

    def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
    ) -> Message:
        del tools
        self.requests.append([message.model_copy(deep=True) for message in messages])
        return Message.assistant("done")


def _skill(tmp_path: Path, name: str) -> SkillSpec:
    root = tmp_path / name
    root.mkdir()
    skill_md = root / "SKILL.md"
    skill_md.write_text(f"instructions for {name}", encoding="utf-8")
    return SkillSpec(
        name=name,
        description=f"Use {name}.",
        root=root,
        skill_md_path=skill_md,
    )


def test_extract_skill_mentions_preserves_order_and_deduplicates() -> None:
    assert extract_skill_mentions(
        "Use $review, then $frontend-design and $review. Keep $HOME intact."
    ) == ["review", "frontend-design"]


def test_extract_skill_mentions_ignores_literal_and_prefix_cases() -> None:
    assert extract_skill_mentions(
        r"Ignore \$review, `use $review`, $review_extra, $review.md, "
        "$review/path, and [$review](/tmp/SKILL.md). Use ($review).\n"
        "```sh\necho $review\n```"
    ) == ["review"]


def test_runtime_activates_multiple_known_mentions_before_user_prompt(
    tmp_path: Path,
) -> None:
    registry = SkillRegistry(
        [_skill(tmp_path, "review"), _skill(tmp_path, "frontend-design")]
    )
    provider = _CaptureProvider()
    agent = RuntimeAgent(
        provider=provider,
        tools=[],
        skill_registry=registry,
        available_skills=registry.skills,
    )

    result = agent.run(
        "$review use $frontend-design on this page; leave $unknown as text."
    )

    assert result.output == "done"
    assert [skill.name for skill in agent.active_skills] == [
        "review",
        "frontend-design",
    ]
    request = provider.requests[0]
    assert [message.role for message in request[-3:]] == ["system", "system", "user"]
    assert "instructions for review" in (request[-3].plain_text_content or "")
    assert "instructions for frontend-design" in (request[-2].plain_text_content or "")
    assert request[-1].plain_text_content == (
        "$review use $frontend-design on this page; leave $unknown as text."
    )


def test_runtime_reactivates_a_skill_once_per_mentioning_prompt(tmp_path: Path) -> None:
    registry = SkillRegistry([_skill(tmp_path, "review")])
    provider = _CaptureProvider()
    agent = RuntimeAgent(
        provider=provider,
        tools=[],
        skill_registry=registry,
        available_skills=registry.skills,
    )

    agent.run("$review inspect this")
    agent.run("Use $review again")

    assert [skill.name for skill in agent.active_skills] == ["review", "review"]
    assert sum(entry.kind == "skill_event" for entry in agent.conversation_entries) == 2
