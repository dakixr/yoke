"""Shared skill activation state transitions."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from yoke.agent.skills.models import ActiveSkill
from yoke.agent.skills.registry import SkillRegistry


@dataclass(frozen=True)
class SkillActivationResult:
    """Result of applying skill activation requests."""

    active_skills: list[ActiveSkill]
    loaded: list[str]
    reloaded: list[str]
    missing: list[str]

    @property
    def ok(self) -> bool:
        """Return whether all requested skills were found."""
        return not self.missing

    def active_payload(self) -> list[dict[str, object]]:
        """Return active skill state suitable for tool result JSON."""
        return [
            skill.model_dump(mode="json")
            for skill in sorted(self.active_skills, key=lambda item: item.name)
        ]


def activate_skills(
    *,
    registry: SkillRegistry,
    active_skills: Sequence[ActiveSkill],
    names: Sequence[str],
) -> SkillActivationResult:
    """Apply skill activation requests to the current active skill state."""
    next_active = [skill.model_copy(deep=True) for skill in active_skills]
    active_by_name = {skill.name: skill for skill in next_active}
    loaded: list[str] = []
    missing: list[str] = []
    reloaded: list[str] = []
    seen_requests: set[str] = set()

    for raw_name in names:
        name = raw_name.strip()
        if not name or name in seen_requests:
            continue
        seen_requests.add(name)
        if registry.get(name) is None:
            missing.append(name)
            continue
        existing = active_by_name.get(name)
        if existing is not None:
            existing.reload_on_next_use = True
            reloaded.append(name)
            continue
        active_skill = registry.activate(name)
        active_skill.reload_on_next_use = True
        if active_skill.is_inline:
            active_skill.content = active_skill.load_content()
        next_active.append(active_skill)
        active_by_name[name] = active_skill
        loaded.append(name)

    return SkillActivationResult(
        active_skills=next_active,
        loaded=loaded,
        reloaded=reloaded,
        missing=missing,
    )
