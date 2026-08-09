from __future__ import annotations

# ruff: noqa: D100,D101,D102,D103,S101

from pathlib import Path

from yoke.agent.loop import RuntimeAgent
from yoke.agent.loop.state import context_for_run
from yoke.agent.models import ConversationEntry
from yoke.agent.models import MemorySnapshot
from yoke.agent.models import Message
from yoke.agent.skills.models import SkillSpec
from yoke.agent.skills.registry import SkillRegistry
from yoke.ai.providers.base import Provider
from yoke.cli.interactive.skill_commands import handle_skill_load
from yoke.cli.render import build_console
from yoke.cli.runtime import ActiveSession
from yoke.cli.session import SessionStore

from .support import CaptureStream


class SkillCommandProvider(Provider):
    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        del messages, tools
        return Message.assistant("unused")


def test_skill_command_appends_skill_message_to_compacted_branch(
    tmp_path: Path,
) -> None:
    instruction = ConversationEntry(
        kind="instruction",
        message=Message.system("original instructions"),
    )
    user = ConversationEntry(
        kind="user",
        message=Message.user("large historical request"),
        parent_id=instruction.id,
    )
    assistant = ConversationEntry(
        kind="assistant",
        message=Message.assistant("large historical response"),
        parent_id=user.id,
    )
    snapshot = ConversationEntry(
        kind="memory_snapshot",
        metadata=MemorySnapshot(
            id="memory-current",
            summary_text="Compacted history",
        ).model_dump(),
        parent_id=assistant.id,
    )
    entries = [instruction, user, assistant, snapshot]
    store = SessionStore(directory=tmp_path / "sessions")
    store.save(
        "skill-refresh",
        [entry.message for entry in entries if entry.message is not None],
        conversation_entries=entries,
        leaf_id=snapshot.id,
        root=tmp_path,
    )
    record = store.load("skill-refresh")
    active_session = ActiveSession(
        id=record.id,
        root=tmp_path,
        store=store,
        record=record,
    )
    skill_path = tmp_path / "review" / "SKILL.md"
    skill_path.parent.mkdir()
    skill_path.write_text("# Review\n", encoding="utf-8")
    skill = SkillSpec(
        name="review",
        description="Review changes",
        root=skill_path.parent,
        skill_md_path=skill_path,
    )
    registry = SkillRegistry([skill])
    agent = RuntimeAgent(
        provider=SkillCommandProvider(),
        tools=[],
        skill_registry=registry,
        active_skills=[registry.activate("review")],
        conversation_entries=entries,
    )
    stale_projection = [
        *record.messages,
        Message.system("stale derived instruction"),
    ]

    handle_skill_load(
        "/skill review",
        agent,
        active_session,
        stale_projection,
        build_console(CaptureStream()),
    )

    updated = store.load("skill-refresh")
    assert len(updated.conversation_entries) == len(entries) + 1
    skill_event = updated.conversation_entries[-1]
    assert updated.leaf_id == skill_event.id
    assert skill_event.kind == "skill_event"
    assert skill_event.parent_id == snapshot.id
    assert skill_event.message is not None
    assert "# Review" in (skill_event.message.plain_text_content or "")
    assert len(updated.active_skills) == 2
    assert updated.conversation_entries[:-1] == entries
    assert all(
        entry.message != stale_projection[-1] for entry in updated.conversation_entries
    )
    assert updated.active_skills[-1].name == "review"
    assert updated.active_skills[-1].content == "# Review\n"
    assert updated.active_skills[-1].activation_id == skill_event.metadata.get(
        "skill_activation_id"
    )
    assert agent._context is not None
    provider_text = "\n".join(
        message.plain_text_content or ""
        for message in agent.context_manager.messages_for_provider(agent._context)
    )
    assert "Compacted history" in provider_text
    assert "large historical request" in provider_text
    assert "stale derived instruction" not in provider_text

    resumed_agent = RuntimeAgent(
        provider=SkillCommandProvider(),
        tools=[],
        skill_registry=registry,
        active_skills=updated.active_skills,
        conversation_entries=updated.conversation_entries,
    )
    resumed_context = context_for_run(
        resumed_agent,
        "continue",
        user_message=None,
        available_skills=None,
        active_skills=None,
    )
    assert (
        sum(
            entry.kind == "skill_event"
            for entry in resumed_context.conversation_log.entries
        )
        == 1
    )
