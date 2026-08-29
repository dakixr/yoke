"""Small persistence helpers shared by the HTTP runtime controller."""

from __future__ import annotations

from collections.abc import Sequence

from yoke.agent.models import ConversationEntry
from yoke.agent.skills.models import ActiveSkill
from yoke.session import SessionRecord
from yoke.session.admissions import INPUT_ID_METADATA_KEY

TURN_SUMMARY_METADATA_KEY = "yoke_turn_summary"
TURN_SUMMARY_MIN_SECONDS = 60.0


def active_skill_list(value: object | None) -> list[ActiveSkill] | None:
    """Return a defensive typed active-skill list when the value is valid."""
    if not isinstance(value, list | tuple):
        return None
    typed = [skill for skill in value if isinstance(skill, ActiveSkill)]
    if len(typed) != len(value):
        return None
    return [skill.model_copy(deep=True) for skill in typed]


def tag_input_entry(entries: list[ConversationEntry], input_id: str) -> None:
    """Attach an admission id to the newest persisted user entry."""
    if any(entry.metadata.get(INPUT_ID_METADATA_KEY) == input_id for entry in entries):
        return
    for entry in reversed(entries):
        if entry.kind == "user" and entry.message is not None:
            entry.metadata[INPUT_ID_METADATA_KEY] = input_id
            return


def with_turn_summary(
    entries: list[ConversationEntry],
    *,
    duration_seconds: float,
    tool_count: int,
) -> list[ConversationEntry]:
    """Return entries with the CLI-style long-turn summary on the current leaf."""
    if duration_seconds < TURN_SUMMARY_MIN_SECONDS or not entries:
        return entries
    copied = list(entries)
    leaf = entries[-1].model_copy(deep=True)
    leaf.metadata[TURN_SUMMARY_METADATA_KEY] = {
        "duration_seconds": duration_seconds,
        "tool_count": tool_count,
    }
    copied[-1] = leaf
    return copied


def input_is_persisted(record: SessionRecord, input_id: str) -> bool:
    """Return whether a saved session already contains one admission id."""
    return any(
        entry.metadata.get(INPUT_ID_METADATA_KEY) == input_id
        for entry in record.conversation_entries
    )


def input_has_terminal_assistant(
    active: Sequence[ConversationEntry],
    input_id: str,
) -> bool:
    """Return whether an input already has a terminal assistant response."""
    seen_input = False
    for entry in active:
        if entry.metadata.get(INPUT_ID_METADATA_KEY) == input_id:
            seen_input = True
            continue
        if not seen_input or entry.kind != "assistant" or entry.message is None:
            continue
        if not entry.message.tool_calls and entry.message.phase != "commentary":
            return True
    return False
