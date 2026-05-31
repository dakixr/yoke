"""Tool for loading agent skills at runtime."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import Field
from pydantic import PrivateAttr

from yoke.agent.models import AgentContext
from yoke.agent.skills.models import ActiveSkill
from yoke.agent.skills.registry import SkillRegistry
from yoke.agent.tools.base import LocalTool


class SkillTool(LocalTool):
    """Tool that loads named skills into the agent context."""

    name = "skill"
    description = (
        "Load skills by name. Use this to activate reusable skill "
        "instructions when they are relevant to the task."
    )

    load: list[str] = Field(default_factory=list)

    _registry: SkillRegistry = PrivateAttr()

    def _bind_context(self, **context: object) -> None:
        super()._bind_context(**context)
        registry = context.get("skill_registry")
        if registry is None or not isinstance(registry, SkillRegistry):
            raise ValueError("skill_registry is required for SkillTool")
        self._registry = registry

    def execute(self) -> dict[str, object]:
        """Process load requests and return the updated skill set."""
        raw_active_skills = self._context.get("active_skills", [])
        active_skills = (
            raw_active_skills if isinstance(raw_active_skills, Sequence) else []
        )
        active_by_name = {
            skill.name: skill
            for skill in active_skills
            if isinstance(skill, ActiveSkill)
        }
        loaded: list[str] = []
        missing: list[str] = []
        reloaded: list[str] = []
        next_active = dict(active_by_name)

        for name in self.load:
            if self._registry.get(name) is None:
                missing.append(name)
                continue
            if name in next_active:
                next_active[name].reload_on_next_use = True
                reloaded.append(name)
                continue
            next_active[name] = self._registry.activate(name)
            next_active[name].reload_on_next_use = True
            if next_active[name].is_inline:
                next_active[name].content = next_active[name].load_content()
                loaded.append(name)

        return {
            "ok": True,
            "loaded": loaded,
            "reloaded": reloaded,
            "missing": missing,
            "active": [
                skill.model_dump(mode="json")
                for skill in sorted(
                    next_active.values(),
                    key=lambda item: item.name,
                )
            ],
        }

    def apply_result(
        self,
        context: AgentContext,
        result: dict[str, object],
    ) -> None:
        """Update the agent context's active skills based on the tool result."""
        raw_active = result.get("active", [])
        if not isinstance(raw_active, list):
            return
        next_active: list[ActiveSkill] = []
        for raw_skill in raw_active:
            if not isinstance(raw_skill, dict):
                continue
            next_active.append(ActiveSkill.model_validate(raw_skill))
        context.active_skills = next_active
        self._context["active_skills"] = list(context.active_skills)
