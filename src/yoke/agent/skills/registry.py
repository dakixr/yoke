"""Skill registry and loading helpers."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from yoke.agent.skills.discovery import SkillLoadFailure
from yoke.agent.skills.discovery import discover_skills
from yoke.agent.skills.discovery import discover_skills_with_failures
from yoke.agent.skills.models import ActiveSkill
from yoke.agent.skills.models import SkillSpec


def _unavailable_skill_content(skill: SkillSpec, error: Exception) -> str:
    return (
        f"Skill content unavailable for `{skill.name}`. The original skill "
        f"file `{skill.skill_md_path}` could not be read: {error}"
    )


class SkillRegistry:
    """Registry that holds available skills and activates them by name."""

    def __init__(
        self,
        skills: list[SkillSpec],
        *,
        failures: Sequence[SkillLoadFailure] = (),
    ) -> None:
        self._skills = {skill.name: skill for skill in skills}
        self._failures = list(failures)

    @property
    def skills(self) -> list[SkillSpec]:
        """Return all registered skill specs."""
        return list(self._skills.values())

    @property
    def failures(self) -> list[SkillLoadFailure]:
        """Return isolated failures encountered while discovering skills."""
        return list(self._failures)

    def get(self, name: str) -> SkillSpec | None:
        """Return the skill spec for the given name, or None if not found."""
        return self._skills.get(name)

    def require(self, name: str) -> SkillSpec:
        """Return the skill spec or raise ValueError if it is not registered."""
        skill = self.get(name)
        if skill is None:
            available = ", ".join(sorted(self._skills)) or "none"
            raise ValueError(f"Unknown skill `{name}`. Available skills: {available}.")
        return skill

    def activate(self, name: str) -> ActiveSkill:
        """Load and return an ActiveSkill for the given skill name."""
        skill = self.require(name)
        try:
            content = skill.load_content()
        except ValueError as exc:
            content = _unavailable_skill_content(skill, exc)
        return ActiveSkill(
            name=skill.name,
            description=skill.description,
            source_path=str(skill.skill_md_path),
            content=content,
        )


def load_skill_registry(
    skill_dirs: Sequence[str | Path],
    *,
    strict: bool = True,
) -> SkillRegistry:
    """Create a SkillRegistry from the given list of skill directories."""
    resolved_dirs = [Path(path).resolve() for path in skill_dirs]
    if strict:
        return SkillRegistry(discover_skills(resolved_dirs))
    result = discover_skills_with_failures(resolved_dirs)
    return SkillRegistry(result.skills, failures=result.failures)
