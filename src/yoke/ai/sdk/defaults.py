"""Convenient defaults for programmatically constructed coding agents."""

from __future__ import annotations

from pathlib import Path

from yoke.agent.capabilities import default_capabilities
from yoke.ai.sdk.types import RunConfig


def default_coding_agent_config(root: str | Path | None = None) -> RunConfig:
    """Return the standard coding-agent configuration used by the SDK."""
    return RunConfig(
        root=Path.cwd() if root is None else root,
        capabilities=default_capabilities(),
    )
