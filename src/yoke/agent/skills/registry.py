"""Registry for managing and activating skills by name."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from yoke.agent.skills.discovery import SkillLoadFailure
from yoke.agent.skills.discovery import discover_skills
from yoke.agent.skills.discovery import discover_skills_with_failures
from yoke.agent.skills.models import ActiveSkill
from yoke.agent.skills.models import SkillSpec


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

    def with_skills(self, skills: Sequence[SkillSpec]) -> SkillRegistry:
        """Return a registry overlaid with additional current skill specs."""
        merged = dict(self._skills)
        merged.update((skill.name, skill) for skill in skills)
        return SkillRegistry(list(merged.values()), failures=self._failures)

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
            content=skill.load_content(),
            file_paths=skill.file_paths,
            reload_on_next_use=True,
        )

    def reconcile(
        self,
        active_skills: Sequence[ActiveSkill] | None,
        fallback: Sequence[ActiveSkill] = (),
    ) -> list[ActiveSkill]:
        """Restore durable active skills against the currently valid registry."""
        if active_skills is None:
            return [skill.model_copy(deep=True) for skill in fallback]
        reconciled: list[ActiveSkill] = []
        for saved in active_skills:
            if saved.is_inline:
                if isinstance(saved.content, str) and saved.content.strip():
                    reconciled.append(saved.model_copy(deep=True))
                continue
            current = self.get(saved.name)
            if current is not None:
                try:
                    refreshed = self.activate(saved.name)
                except ValueError:
                    pass
                else:
                    refreshed.reload_on_next_use = saved.reload_on_next_use
                    reconciled.append(refreshed)
                    continue
            if isinstance(saved.content, str) and saved.content.strip():
                snapshot = saved.model_copy(deep=True)
                snapshot.reload_on_next_use = True
                snapshot.file_paths = [
                    path for path in snapshot.file_paths if Path(path).is_file()
                ]
                reconciled.append(snapshot)
        return reconciled


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
