"""Tool for loading agent skills at runtime."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import Field
from pydantic import PrivateAttr

from yoke.agent.models import AgentContext
from yoke.agent.skills.activation import activate_skills
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
        return {
            "ok": True,
            "requested": list(self.load),
            "loaded": [],
            "reloaded": [],
            "missing": [],
            "active": [],
        }

    def apply_result(
        self,
        context: AgentContext,
        result: dict[str, object],
    ) -> None:
        """Update the agent context's active skills based on the tool result."""
        raw_requested = result.get("requested", [])
        requested = raw_requested if isinstance(raw_requested, Sequence) else []
        activation = activate_skills(
            registry=self._registry,
            active_skills=context.active_skills,
            names=[name for name in requested if isinstance(name, str)],
        )
        context.active_skills = activation.active_skills
        self._context["active_skills"] = list(context.active_skills)
        result["ok"] = activation.ok
        result["loaded"] = activation.loaded
        result["reloaded"] = activation.reloaded
        result["missing"] = activation.missing
        result["active"] = activation.active_payload()
