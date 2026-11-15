"""Native agent ownership for deterministic HTTP turn fakes."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Self

from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import ConversationEntry, Message
from yoke.agent.skills.models import ActiveSkill, SkillSpec
from yoke.session import SessionRecord


class _UnusedProvider:
    supports_image_inputs = True

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        raise AssertionError("a fake turn must override run")


class FakeSessionAgent(RuntimeAgent):
    """Use native tools/context/cleanup while tests supply deterministic turns."""

    def __init__(self, record: SessionRecord) -> None:
        super().__init__(
            _UnusedProvider(), [], tool_root=Path(record.root) if record.root else None
        )
        self.context_manager.max_total_tokens = record.context_window_tokens
        self.fake_record = record
        self.fake_is_turn = False
        self.fake_native_fields = {*vars(self), "fake_native_fields"}

    def fork(
        self, *, isolate_provider: bool = False, include_state: bool = True
    ) -> Self:
        forked = type(self).__new__(type(self))
        FakeSessionAgent.__init__(forked, self.fake_record)
        for name, value in vars(self).items():
            if name not in self.fake_native_fields:
                setattr(forked, name, value)
        forked.fake_is_turn = True
        return forked

    def load_owned_conversation(
        self,
        conversation_entries: list[ConversationEntry],
        *,
        available_skills: Sequence[SkillSpec] | None = None,
        active_skills: Sequence[ActiveSkill] | None = None,
    ) -> None:
        super().load_owned_conversation(
            conversation_entries,
            available_skills=available_skills,
            active_skills=active_skills,
        )
        self.record = self.fake_record.model_copy(
            update={
                "conversation_entries": conversation_entries,
                "leaf_id": conversation_entries[-1].id
                if conversation_entries
                else None,
            }
        )
