"""Default SDK agent configurations."""

from __future__ import annotations

from pathlib import Path

from yoke.ai.sdk.types import AgentTool
from yoke.ai.sdk.types import RunConfig

DEFAULT_CODING_AGENT_PROMPT = (
    "You are a focused coding agent. Inspect the workspace, make minimal "
    "correct changes, prefer patches over broad rewrites, and report changed "
    "files plus any validation performed."
)


def default_coding_agent_tools() -> list[AgentTool]:
    """Return the default SDK coding-agent tool set."""
    return [
        "image.attach",
        "image.generate",
        "file.search",
        "file.read",
        "web.fetch",
        "web.search",
        "web.research",
        "file.write",
        "shell",
    ]


def default_coding_agent_config(root: str | Path | None = None) -> RunConfig:
    """Build the default SDK coding-agent configuration."""
    return RunConfig(
        root=Path.cwd() if root is None else root,
        sys_prompt=DEFAULT_CODING_AGENT_PROMPT,
        tools=default_coding_agent_tools(),
    )
