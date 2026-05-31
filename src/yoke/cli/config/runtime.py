"""Runtime config and provider selection for the yoke CLI."""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from yoke.agent.loop import MaxIterationsExceededError
from yoke.agent.loop import RuntimeAgent
from yoke.agent.skills import ActiveSkill
from yoke.agent.skills import SkillRegistry
from yoke.agent.skills import load_skill_registry
from yoke.ai.providers.base import ProviderError
from yoke.agent.budget import build_provider_context_manager
from yoke.cli.bootstrap.config import resolve_agent_config
from yoke.cli.bootstrap.types import ToolLoadReport
from yoke.cli.config.providers import build_provider_from_args
from yoke.cli.config.providers import prepare_provider_args

if TYPE_CHECKING:
    from yoke.cli.bootstrap.types import ResolvedAgentConfig

harness_root = Path(__file__).parent.parent.parent

DEFAULT_SYSTEM_PROMPT = (
    (Path(__file__).parent.parent / "sys_prompt.md")
    .read_text(encoding="utf-8")
    .replace("{harness_root}", str(harness_root))
    .replace("{global_yoke_dir}", str(Path.home() / ".yoke"))
)

RUN_ERRORS = (ProviderError, MaxIterationsExceededError)


@dataclass(slots=True)
class CLIArgs:
    """CLIArgs."""

    prompt: str | None = None
    headless: bool = False
    session: str | None = None
    model: str | None = None
    provider_name: str | None = None
    provider_from_default: bool = False
    reasoning_effort: str | None = None
    root: str = os.getcwd()
    skills: tuple[str, ...] = ()
    images: tuple[str, ...] = ()


@dataclass(slots=True)
class BuiltCLIAgent:
    """BuiltCLIAgent."""

    agent: RuntimeAgent
    tool_report: ToolLoadReport


def build_agent_from_args(args: CLIArgs) -> RuntimeAgent:
    """build_agent_from_args."""
    return build_cli_agent_from_args(args).agent


def build_cli_agent_from_args(args: CLIArgs) -> BuiltCLIAgent:
    """build_cli_agent_from_args."""
    prepare_provider_args(args)
    skill_registry = _load_cli_skill_registry(Path(args.root))
    initial_active_skills = _activate_cli_skills(skill_registry, args.skills)
    resolved = _resolve_cli_agent_config(
        root=Path(args.root),
        skill_registry=skill_registry,
        active_skills=initial_active_skills,
    )

    provider = build_provider_from_args(args)
    for tool in resolved.tools:
        tool._context["provider"] = provider
    context_manager = build_provider_context_manager(
        provider=provider,
        instructions=resolved.system_messages,
    )

    agent = RuntimeAgent(
        provider=provider,
        tools=list(resolved.tools),
        max_iterations=42_000_000,
        context_manager=context_manager,
        skill_registry=skill_registry,
        available_skills=(skill_registry.skills if skill_registry is not None else []),
        active_skills=initial_active_skills,
    )
    agent.tool_report = resolved.tool_report
    return BuiltCLIAgent(agent=agent, tool_report=resolved.tool_report)


def default_cli_skill_dirs(root: Path) -> list[str]:
    """default_cli_skill_dirs."""
    home = Path.home().resolve()
    candidates = set([root / ".yoke" / "skills", home / ".yoke" / "skills"])
    return [str(path.resolve()) for path in candidates if path.is_dir()]


def build_tool_report(*, root: Path) -> ToolLoadReport:
    """build_tool_report."""
    return _resolve_cli_agent_config(
        root=root,
        skill_registry=_load_cli_skill_registry(root),
        active_skills=[],
    ).tool_report


def format_tool_discovery_message(report: ToolLoadReport) -> str:
    """format_tool_discovery_message."""
    message = (
        f"Loaded {report.count('default')} builtin tools, "
        f"{report.count('repo')} repo tools from .yoke, "
        f"{report.count('global')} global tools from ~/.yoke"
    )
    config_denied_count = len(report.denied_tools)
    if config_denied_count:
        message += f", {config_denied_count} denied by config"
    return message


def _load_cli_skill_registry(root: Path) -> SkillRegistry | None:
    skill_dirs = default_cli_skill_dirs(root)
    return load_skill_registry(skill_dirs) if skill_dirs else None


def _activate_cli_skills(
    skill_registry: SkillRegistry | None,
    skill_names: tuple[str, ...],
) -> list[ActiveSkill]:
    if skill_registry is None:
        return []
    return [skill_registry.activate(name) for name in skill_names]


def _resolve_cli_agent_config(
    *,
    root: Path,
    skill_registry: SkillRegistry | None,
    active_skills: Sequence[ActiveSkill],
) -> ResolvedAgentConfig:
    resolved = resolve_agent_config(
        root=root,
        base_system_prompt=DEFAULT_SYSTEM_PROMPT,
        include_repo_tools=True,
        include_global_tools=True,
    )
    if skill_registry is None:
        return resolved
    from yoke.agent.tools import SkillTool
    from yoke.cli.bootstrap.types import ResolvedAgentConfig

    skill_tool = SkillTool.bind(
        skill_registry=skill_registry,
        active_skills=list(active_skills),
    )
    return ResolvedAgentConfig(
        system_messages=list(resolved.system_messages),
        tools=[*resolved.tools, skill_tool],
        tool_report=resolved.tool_report,
    )


def format_provider_model_status(agent: object) -> str | None:
    """format_provider_model_status."""
    provider = getattr(agent, "provider", None)
    if provider is None:
        return None
    provider_name = provider.__class__.__name__
    config = getattr(provider, "config", None)
    model = getattr(config, "model", None)
    reasoning_effort = getattr(config, "reasoning_effort", None)
    if not isinstance(model, str) or not model.strip():
        base = provider_name
    else:
        base = f"{provider_name} {model.strip()}"
    if isinstance(reasoning_effort, str) and reasoning_effort.strip():
        return f"{base} {reasoning_effort.strip()}"
    return base
