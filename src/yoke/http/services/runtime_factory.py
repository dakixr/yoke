"""Build provider/tool runtimes for HTTP-owned sessions."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from yoke.agent.loop.agent import RuntimeAgent
from yoke.cli.config import CLIArgs
from yoke.cli.config.runtime import build_cli_agent_from_args
from yoke.cli.runtime.session import apply_session_defaults_to_args
from yoke.session import SessionRecord


type SessionAgentFactory = Callable[[SessionRecord], object]


def build_http_session_agent(record: SessionRecord) -> RuntimeAgent:
    """Build the same configured agent used by the CLI for one saved session."""
    if not record.root:
        raise ValueError("Session does not have a workspace root.")
    root = Path(record.root).resolve()
    args = CLIArgs(session=record.id, root=str(root))
    apply_session_defaults_to_args(args, record)
    built = build_cli_agent_from_args(args)
    agent = built.agent
    if record.active_skills:
        agent.active_skills = [
            skill.model_copy(deep=True) for skill in record.active_skills
        ]
    # SessionRuntime owns loading the active conversation into either the
    # primary agent or an isolated turn fork. Loading it here as well makes a
    # cold large-session turn materialize the same history twice before the
    # provider can start.
    return agent
