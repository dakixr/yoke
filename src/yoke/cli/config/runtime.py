"""Runtime config and provider selection for the yoke CLI."""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Literal

from yoke.agent.loop.agent import RuntimeAgent
from yoke.agent.skills import ActiveSkill
from yoke.agent.skills import SkillRegistry
from yoke.agent.skills import load_skill_registry
from yoke.agent.skills.paths import default_skill_dirs
from yoke.ai.providers.base import ProviderError
from yoke.ai.providers.base import Provider
from yoke.ai.providers.model_selection import current_model_id_from_config
from yoke.ai.providers.resolution import UnknownModelError
from yoke.agent.tools import ToolRegistrationContext
from yoke.agent.tools import ToolRegistrationResult
from yoke.agent.budget import build_provider_context_manager
from yoke.cli.bootstrap.config import ToolDiscoveryProvider
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

RUN_ERRORS = (ProviderError,)


@dataclass(slots=True)
class CLIArgs:
    """CLIArgs."""

    prompt: str | None = None
    headless: bool = False
    session: str | None = None
    fork_session_id: str | None = None
    model: str | None = None
    provider_name: str | None = None
    reasoning_effort: str | None = None
    root: str = os.getcwd()
    skills: tuple[str, ...] = ()
    images: tuple[str, ...] = ()
    model_source: Literal["cli", "config", "session"] | None = None


@dataclass(slots=True)
class BuiltCLIAgent:
    """BuiltCLIAgent."""

    agent: RuntimeAgent
    tool_report: ToolLoadReport
    startup_warning: str | None = None


def build_agent_from_args(args: CLIArgs) -> RuntimeAgent:
    """build_agent_from_args."""
    return build_cli_agent_from_args(args).agent


def build_cli_agent_from_args(
    args: CLIArgs, *, recover_model: bool = False
) -> BuiltCLIAgent:
    """Build a CLI runtime, optionally recovering an inherited stale model."""
    provider, warning = _build_startup_provider(args, recover_model=recover_model)
    try:
        skill_registry = _load_cli_skill_registry(Path(args.root))
        initial_active_skills = _activate_cli_skills(skill_registry, args.skills)
        built = _build_cli_agent(
            args,
            provider=provider,
            skill_registry=skill_registry,
            initial_active_skills=initial_active_skills,
        )
        built.startup_warning = warning
        return built
    except BaseException:
        close = getattr(provider, "close", None)
        if callable(close):
            try:
                close()
            except BaseException:
                pass
        raise


def _build_startup_provider(
    args: CLIArgs, *, recover_model: bool
) -> tuple[Provider, str | None]:
    try:
        prepare_provider_args(args)
        return build_provider_from_args(args), None
    except UnknownModelError as exc:
        if (
            not recover_model
            or args.headless
            or args.model_source not in {"config", "session"}
            or args.model != exc.model_id
            or args.provider_name not in {None, exc.provider_name}
        ):
            raise
        # Retry exactly once, on the same provider. Do not reload the stale
        # config or carry its model-specific reasoning effort into the default.
        fallback_args = replace(
            args, provider_name=exc.provider_name, model=None, reasoning_effort=None
        )
        provider = build_provider_from_args(fallback_args)
        config = getattr(provider, "config", None)
        model = current_model_id_from_config(config)
        effort = getattr(config, "reasoning_effort", None)
        args.provider_name = exc.provider_name
        args.model = model
        args.reasoning_effort = effort if isinstance(effort, str) else None
        replacement = (
            f"{exc.provider_name}:{model}"
            if model is not None
            else f"the default model for {exc.provider_name}"
        )
        if args.model_source == "config":
            warning = (
                f"Configured model {exc.provider_name}:{exc.model_id} is no longer "
                f"available. Started with {replacement}. The configured default "
                "was not changed. Use /model or `yoke models set` to choose another "
                "model."
            )
        else:
            warning = (
                f"Saved model {exc.provider_name}:{exc.model_id} is no longer "
                f"available. Resumed with {replacement}. Use /model to choose "
                "another model."
            )
        return provider, warning


def _build_cli_agent(
    args: CLIArgs,
    *,
    provider: Provider,
    skill_registry: SkillRegistry | None,
    initial_active_skills: list[ActiveSkill],
) -> BuiltCLIAgent:
    """Build a CLI runtime after its provider has been created."""
    root = Path(args.root).resolve()
    initial_resolution = _resolve_cli_agent_config(
        root=root,
        skill_registry=skill_registry,
        active_skills=initial_active_skills,
        provider=provider,
    )
    agent_holder: list[RuntimeAgent] = []
    report_holder = [initial_resolution.tool_report]
    pending_initial_resolution = [initial_resolution]

    def tool_factory(
        context: ToolRegistrationContext,
    ) -> ToolRegistrationResult:
        active_skills = (
            agent_holder[0].active_skills if agent_holder else initial_active_skills
        )
        if (
            not agent_holder
            and pending_initial_resolution
            and context.provider is provider
            and context.root == root
        ):
            resolved = pending_initial_resolution.pop()
        else:
            pending_initial_resolution.clear()
            resolved = _resolve_cli_agent_config(
                root=root,
                skill_registry=skill_registry,
                active_skills=active_skills,
                provider=context.provider,
            )
        report_holder[:] = [resolved.tool_report]
        if agent_holder:
            agent_holder[0].tool_report = resolved.tool_report
        return ToolRegistrationResult(
            tools=resolved.tools,
            system_messages=resolved.tool_system_messages or [],
        )

    context_manager = build_provider_context_manager(
        provider=provider,
        instructions=initial_resolution.system_messages,
    )

    agent = RuntimeAgent(
        provider=provider,
        tools=[],
        tool_factory=tool_factory,
        tool_root=root,
        tool_home=Path.home().resolve(),
        context_manager=context_manager,
        skill_registry=skill_registry,
        available_skills=(skill_registry.skills if skill_registry is not None else []),
        active_skills=initial_active_skills,
    )
    agent_holder.append(agent)
    tool_report = report_holder[0]
    agent.tool_report = tool_report
    return BuiltCLIAgent(agent=agent, tool_report=tool_report)


def default_cli_skill_dirs(root: Path) -> list[str]:
    """default_cli_skill_dirs."""
    return default_skill_dirs(root)


def build_tool_report(*, root: Path) -> ToolLoadReport:
    """build_tool_report."""
    return _resolve_cli_agent_config(
        root=root,
        skill_registry=_load_cli_skill_registry(root),
        active_skills=[],
        provider=ToolDiscoveryProvider(),
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
    return load_skill_registry(skill_dirs)


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
    provider: Provider,
) -> ResolvedAgentConfig:
    resolved = resolve_agent_config(
        root=root,
        base_system_prompt=DEFAULT_SYSTEM_PROMPT,
        include_repo_tools=True,
        include_global_tools=True,
        provider=provider,
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
        tool_system_messages=list(resolved.tool_system_messages or []),
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
