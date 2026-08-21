"""MCP-only facade for reading configured agent skills."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

from pydantic import Field
from pydantic import PrivateAttr

from yoke.agent.skills.discovery import builtin_skill_dir
from yoke.agent.skills.discovery import load_skill
from yoke.agent.skills.models import SkillSpec
from yoke.agent.skills.registry import SkillRegistry
from yoke.agent.tools.base import LocalTool

logger = logging.getLogger(__name__)


def load_mcp_skill_registry(skill_dirs: Sequence[Path]) -> SkillRegistry:
    """Discover configured skills recursively, with built-ins as fallbacks."""
    discovered: dict[str, SkillSpec] = {}
    for skill_dir in (*skill_dirs, builtin_skill_dir()):
        resolved_dir = skill_dir.expanduser().resolve()
        if not resolved_dir.is_dir():
            continue
        for skill_md_path in sorted(resolved_dir.rglob("SKILL.md")):
            spec = load_skill(skill_md_path.parent)
            previous = discovered.get(spec.name)
            if previous is not None:
                logger.info(
                    "Ignoring duplicate MCP skill %s at %s; using %s",
                    spec.name,
                    spec.skill_md_path,
                    previous.skill_md_path,
                )
                continue
            discovered[spec.name] = spec
    return SkillRegistry(list(discovered.values()))


class MCPSkillTool(LocalTool):
    """Read skill instructions and file paths without mutating agent context."""

    name = "skill"
    description = (
        "Load configured agent skills by name. Returns the complete SKILL.md "
        "instructions and absolute paths for every file under each skill "
        "directory. Pass an empty load list to discover available skills."
    )

    load: list[str] = Field(
        default_factory=list,
        description=(
            "Skill names to load. Leave empty to list the available names and "
            "descriptions."
        ),
    )

    _registry: SkillRegistry = PrivateAttr()

    def _bind_context(self, **context: object) -> None:
        super()._bind_context(**context)
        registry = context.get("skill_registry")
        if not isinstance(registry, SkillRegistry):
            raise ValueError("skill_registry is required for MCPSkillTool")
        self._registry = registry

    def execute(self) -> dict[str, object]:
        """Return a catalog or the requested skill payloads."""
        requested = _unique_names(self.load)
        available = [
            {
                "name": skill.name,
                "description": skill.description,
                "skill_md_path": str(skill.skill_md_path),
            }
            for skill in sorted(self._registry.skills, key=lambda item: item.name)
        ]
        if not requested:
            return {"ok": True, "available": available}

        loaded: list[str] = []
        missing: list[str] = []
        payloads: list[dict[str, object]] = []
        for name in requested:
            spec = self._registry.get(name)
            if spec is None:
                missing.append(name)
                continue
            active = self._registry.activate(name)
            loaded.append(name)
            payloads.append(
                {
                    "name": active.name,
                    "description": active.description,
                    "skill_md_path": active.source_path,
                    "files": active.directory_file_listing(),
                    "content": active.content or "",
                }
            )
        return {
            "ok": not missing,
            "requested": requested,
            "loaded": loaded,
            "missing": missing,
            "skills": payloads,
        }


def _unique_names(names: Sequence[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for raw_name in names:
        name = raw_name.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        unique.append(name)
    return unique
