"""CLI-only startup recovery, warnings, and ownership of constructed agents."""

from __future__ import annotations

import sys
from pathlib import Path

from rich.text import Text

from yoke.cli.bootstrap.types import ToolLoadReport
from yoke.cli.config import CLIArgs
from yoke.cli.config import build_cli_agent_from_args
from yoke.cli.render import OutputStream
from yoke.cli.render import build_console
from yoke.cli.runtime.base import AgentRunner
from yoke.cli.runtime.base import ToolReportAgent
from yoke.cli.runtime.lifetime import register_cli_owned_agent
from yoke.cli.runtime.session import apply_session_defaults_to_args
from yoke.cli.session import SessionStore


def apply_startup_session_defaults(args: CLIArgs) -> None:
    """Read continuation defaults without creating or modifying a session."""
    source_id = args.fork_session_id or args.session
    if source_id is None:
        return
    record = SessionStore().load(source_id)
    if record.created_at is None and not record.conversation_entries:
        if args.fork_session_id is not None:
            raise ValueError(f"Session not found: {source_id}")
        return
    if args.fork_session_id is None and record.root:
        args.root = str(Path(record.root).resolve())
    # An explicit model selection includes its own optional thinking effort.
    # Do not replace it with either part of the saved selection.
    if args.model is None:
        apply_session_defaults_to_args(args, record)


def resolve_runtime_agent(
    args: CLIArgs,
    *,
    agent: AgentRunner | None,
    stderr: OutputStream | None = None,
) -> tuple[AgentRunner, ToolLoadReport | None]:
    """Build an owned CLI agent and report recovery before accepting input."""
    if agent is None:
        built = build_cli_agent_from_args(args, recover_model=True)
        register_cli_owned_agent(built.agent)
        if built.startup_warning:
            build_console(stderr or sys.stderr).print(
                Text(f"Warning: {built.startup_warning}", style="yellow")
            )
        return built.agent, built.tool_report
    tool_report = agent.tool_report if isinstance(agent, ToolReportAgent) else None
    return agent, tool_report


def can_recover_resumed_provider(args: CLIArgs, exc: ValueError) -> bool:
    """Keep the legacy removed-provider recovery limited to saved selections."""
    return (
        not args.headless
        and args.model_source == "session"
        and str(exc).startswith("Unsupported provider ")
    )
