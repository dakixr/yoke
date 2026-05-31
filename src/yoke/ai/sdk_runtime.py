"""Shared SDK helpers for constructing runtime agents."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from yoke.agent.models import Message
from yoke.agent.skills import ActiveSkill
from yoke.agent.skills import SkillRegistry

if TYPE_CHECKING:
    from yoke.agent.tools import LocalTool

    type AgentTool = LocalTool | type[LocalTool]
else:
    type AgentTool = object


def bind_agent_tools(
    tools: Sequence[AgentTool],
    *,
    root: Path,
    skill_registry: SkillRegistry | None = None,
    active_skills: Sequence[ActiveSkill] | None = None,
    enable_skill_tool: bool = True,
) -> list[LocalTool]:
    """Bind user-provided tool classes or instances for runtime execution."""
    from yoke.agent.tools import LocalTool
    from yoke.agent.tools import WorkspaceTool

    bound_tools: list[LocalTool] = []
    for tool in tools:
        if isinstance(tool, LocalTool):
            bound_tools.append(tool)
            continue
        if isinstance(tool, type) and issubclass(tool, LocalTool):
            context = {"root": root} if issubclass(tool, WorkspaceTool) else {}
            bound_tools.append(tool.bind(**context))
            continue
        raise TypeError(
            "Agent tools must be LocalTool instances or LocalTool classes. "
            f"Got {tool!r}."
        )
    if skill_registry is not None and enable_skill_tool:
        from yoke.agent.tools import SkillTool

        bound_tools.append(
            SkillTool.bind(
                skill_registry=skill_registry,
                active_skills=list(active_skills or []),
            )
        )
    return bound_tools


def build_system_messages(
    *,
    root: Path,
    sys_prompt: str | None,
    include_agents_file: bool,
) -> list[Message]:
    """Build runtime system messages from SDK configuration."""
    messages: list[Message] = []
    if sys_prompt:
        messages.append(Message.system(sys_prompt))
    if include_agents_file:
        messages.extend(load_agents_messages(root))
    return messages


def load_agents_messages(root: Path) -> list[Message]:
    """Load AGENTS.md messages for a workspace root."""
    from yoke.cli.bootstrap.agents import load_agents_messages as impl

    return impl(root)
