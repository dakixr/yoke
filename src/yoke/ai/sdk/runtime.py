"""Shared SDK helpers for constructing runtime agents."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from yoke.agent.capabilities import create_builtin_capabilities
from yoke.agent.context import ContextManager
from yoke.agent.loop.agent import RuntimeAgent
from yoke.agent.models import Message
from yoke.agent.skills import ActiveSkill
from yoke.agent.skills import SkillRegistry
from yoke.agent.tools import RegisterTools
from yoke.agent.tools import ToolRegistrationContext
from yoke.agent.tools import ToolRegistrationResult
from yoke.agent.tools import never_cancel
from yoke.agent.tools.context import resolve_model_identity

if TYPE_CHECKING:
    from yoke.agent.tools import LocalTool
    from yoke.ai.providers.base import Provider
    from yoke.ai.sdk.types import RunConfig

    type AgentTool = LocalTool | type[LocalTool] | str
else:
    type AgentTool = object


def build_runtime_agent(
    *,
    provider: Provider,
    config: RunConfig,
) -> RuntimeAgent:
    """Build a runtime agent from the public SDK run configuration."""
    root = Path(config.root).resolve()
    active_skills = [skill.to_active_skill() for skill in config.skills]
    available_skills = [
        skill.to_skill_spec()
        for skill in config.skills
        if skill.source_path != "<inline>"
    ]
    skill_registry = SkillRegistry(available_skills) if available_skills else None
    uses_capabilities = config_contains_capability_ids(config.tools)
    tool_factory = None
    agent_holder: list[RuntimeAgent] = []
    if uses_capabilities:
        tool_factory = _runtime_tool_factory(
            config=config,
            root=root,
            skill_registry=skill_registry,
            active_skills=active_skills,
            agent_holder=agent_holder,
        )
        tools = []
    else:
        tools = bind_agent_tools(
            config.tools,
            root=root,
            provider=provider,
            skill_registry=skill_registry,
            active_skills=active_skills,
        )
    agent = RuntimeAgent(
        provider=provider,
        tools=tools,
        tool_factory=tool_factory,
        tool_root=root if tool_factory is not None else None,
        max_iterations=config.max_iterations,
        context_manager=ContextManager(
            instructions=build_system_messages(
                root=root,
                sys_prompt=config.sys_prompt,
                include_agents_file=config.include_agents_file,
            ),
            compaction_policy=config.compaction,
        ),
        tool_execution=config.tool_execution,
        before_tool_call=config.before_tool_call,
        after_tool_call=config.after_tool_call,
        skill_registry=skill_registry,
        available_skills=available_skills,
        active_skills=active_skills,
        messages=config.messages,
        conversation_entries=config.conversation_entries,
    )
    agent_holder.append(agent)
    return agent


def _runtime_tool_factory(
    *,
    config: RunConfig,
    root: Path,
    skill_registry: SkillRegistry | None,
    active_skills: list[ActiveSkill],
    agent_holder: list[RuntimeAgent],
) -> RegisterTools:
    def tool_factory(
        context: ToolRegistrationContext,
    ) -> ToolRegistrationResult:
        current_active_skills = (
            agent_holder[0].active_skills if agent_holder else active_skills
        )
        return bind_agent_tools_from_context(
            config.tools,
            root=root,
            context=context,
            skill_registry=skill_registry,
            active_skills=current_active_skills,
        )

    return tool_factory


def bind_agent_tools(
    tools: Sequence[AgentTool],
    *,
    root: Path,
    provider: Provider | None = None,
    skill_registry: SkillRegistry | None = None,
    active_skills: Sequence[ActiveSkill] | None = None,
    enable_skill_tool: bool = True,
    registration_context: ToolRegistrationContext | None = None,
) -> list[LocalTool]:
    """Bind user-provided tool classes or instances for runtime execution."""
    return list(
        bind_agent_tools_result(
            tools,
            root=root,
            provider=provider,
            skill_registry=skill_registry,
            active_skills=active_skills,
            enable_skill_tool=enable_skill_tool,
            registration_context=registration_context,
        ).tools
    )


def bind_agent_tools_result(
    tools: Sequence[AgentTool],
    *,
    root: Path,
    provider: Provider | None = None,
    skill_registry: SkillRegistry | None = None,
    active_skills: Sequence[ActiveSkill] | None = None,
    enable_skill_tool: bool = True,
    registration_context: ToolRegistrationContext | None = None,
) -> ToolRegistrationResult:
    """Bind SDK tools and preserve capability system messages."""
    from yoke.agent.tools import LocalTool
    from yoke.agent.tools import WorkspaceTool

    bound_tools: list[LocalTool] = []
    system_messages: list[Message] = []
    for tool in tools:
        if isinstance(tool, str):
            if provider is None:
                raise ValueError("Capability tool IDs require an active provider.")
            registration = _bind_capability_tool(
                tool,
                root=root,
                provider=provider,
                registration_context=registration_context,
            )
            bound_tools.extend(registration.tools)
            system_messages.extend(registration.system_messages)
            continue
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
    return ToolRegistrationResult(
        tools=tuple(bound_tools),
        system_messages=tuple(system_messages),
    )


def config_contains_capability_ids(tools: Sequence[AgentTool]) -> bool:
    """Return whether a RunConfig tool list contains capability IDs."""
    return any(isinstance(tool, str) for tool in tools)


def bind_agent_tools_from_context(
    tools: Sequence[AgentTool],
    *,
    root: Path,
    context,
    skill_registry: SkillRegistry | None = None,
    active_skills: Sequence[ActiveSkill] | None = None,
    enable_skill_tool: bool = True,
) -> ToolRegistrationResult:
    """Bind SDK tools using a provider-aware registration context."""
    return bind_agent_tools_result(
        tools,
        root=root,
        provider=context.provider,
        skill_registry=skill_registry,
        active_skills=active_skills,
        enable_skill_tool=enable_skill_tool,
        registration_context=context,
    )


def _bind_capability_tool(
    capability_id: str,
    *,
    root: Path,
    provider: Provider,
    registration_context: ToolRegistrationContext | None = None,
) -> ToolRegistrationResult:
    context = registration_context or ToolRegistrationContext(
        root=root,
        home=Path.home().resolve(),
        provider=provider,
        model=resolve_model_identity(provider),
        cancel_requested=never_cancel,
    )
    for capability in create_builtin_capabilities(context):
        if capability.capability_id == capability_id:
            return ToolRegistrationResult(
                tools=tuple(capability.tools),
                system_messages=tuple(capability.system_messages),
            )
    raise ValueError(f"Unknown built-in tool capability: {capability_id}")


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
    from yoke.agent.instructions import load_agents_messages as impl

    return impl(root)
