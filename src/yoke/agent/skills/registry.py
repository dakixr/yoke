"""Registry for managing and activating skills by name."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from yoke.agent.skills.discovery import discover_skills
from yoke.agent.skills.models import ActiveSkill
from yoke.agent.skills.models import SkillSpec


class SkillRegistry:
    """Registry that holds available skills and activates them by name."""

    def __init__(self, skills: list[SkillSpec]) -> None:
        self._skills = {skill.name: skill for skill in skills}

    @property
    def skills(self) -> list[SkillSpec]:
        """Return all registered skill specs."""
        return list(self._skills.values())

    def get(self, name: str) -> SkillSpec | None:
        """Return the skill spec for the given name, or None if not found."""
        return self._skills.get(name)

    def require(self, name: str) -> SkillSpec:
        """Return the skill spec for the given name, or raise KeyError."""
        skill = self.get(name)
        if skill is None:
            available = ", ".join(sorted(self._skills))
            if available:
                raise KeyError(
                    f"Unknown skill `{name}`. Available skills: {available}."
                )
            raise KeyError(
                f"Unknown skill `{name}`. No skills are currently available."
            )
        return skill

    def activate(self, name: str) -> ActiveSkill:
        """Load and return an ActiveSkill for the given skill name."""
        skill = self.require(name)
        return ActiveSkill(
            name=skill.name,
            description=skill.description,
            source_path=str(skill.skill_md_path),
            reload_on_next_use=True,
        )


def load_skill_registry(skill_dirs: Sequence[str | Path]) -> SkillRegistry:
    """Create a SkillRegistry from the given list of skill directories."""
    return SkillRegistry(discover_skills([Path(path).resolve() for path in skill_dirs]))
