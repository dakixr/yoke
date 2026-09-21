"""Prompt skill-mention parsing and activation."""

from __future__ import annotations

from collections.abc import Sequence
import re

from yoke.agent.skills.activation import activate_skills
from yoke.agent.skills.models import ActiveSkill
from yoke.agent.skills.registry import SkillRegistry


_MENTION_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-:"
)
_SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def extract_skill_mentions(prompt: str) -> list[str]:
    """Return unique ``$skill-name`` mentions in textual order."""
    names: list[str] = []
    seen: set[str] = set()
    index = 0
    inline_delimiter = 0
    fence_delimiter = 0
    while index < len(prompt):
        if prompt[index] == "`":
            end = index
            while end < len(prompt) and prompt[end] == "`":
                end += 1
            delimiter = end - index
            at_line_start = not prompt[:index].rsplit("\n", 1)[-1].strip()
            if delimiter >= 3 and at_line_start:
                fence_delimiter = 0 if fence_delimiter else delimiter
            elif not fence_delimiter:
                inline_delimiter = 0 if inline_delimiter == delimiter else delimiter
            index = end
            continue
        if fence_delimiter or inline_delimiter:
            index += 1
            continue
        if prompt[index] != "$":
            index += 1
            continue
        preceding = prompt[index - 1] if index else ""
        if preceding and (
            preceding == "\\" or preceding.isalnum() or preceding in "_$"
        ):
            index += 1
            continue
        start = index + 1
        end = start
        while end < len(prompt) and prompt[end] in _MENTION_CHARS:
            end += 1
        if end == start:
            index += 1
            continue
        name = prompt[start:end]
        suffix = prompt[end : end + 2]
        linked = preceding == "[" and suffix.startswith("](")
        path_like = bool(
            suffix.startswith("/")
            or (suffix.startswith(".") and len(suffix) == 2 and suffix[1].isalnum())
        )
        if (
            _SKILL_NAME_RE.fullmatch(name)
            and not name.isdigit()
            and not linked
            and not path_like
            and name not in seen
        ):
            seen.add(name)
            names.append(name)
        index = end
    return names


def activate_mentioned_skills(
    *,
    prompt: str,
    registry: SkillRegistry | None,
    active_skills: Sequence[ActiveSkill],
) -> list[ActiveSkill]:
    """Activate known prompt mentions once and preserve current skill state."""
    if registry is None:
        return [skill.model_copy(deep=True) for skill in active_skills]
    names = [
        name
        for name in extract_skill_mentions(prompt)
        if registry.get(name) is not None
    ]
    return activate_skills(
        registry=registry,
        active_skills=active_skills,
        names=names,
    ).active_skills
