"""Agent-level durable state persistence operations."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from yoke.agent.persistence.io import read_agent_state_snapshot
from yoke.agent.persistence.io import write_agent_state_snapshot
from yoke.agent.persistence.models import AgentStateSnapshot
from yoke.agent.skills.models import SkillSpec
from yoke.agent.state import AgentState
from yoke.agent.state import capture_agent_state
from yoke.agent.state import hydrate_agent_state


def save_agent_state(
    agent: object,
    path: str | os.PathLike[str],
    *,
    metadata: dict[str, Any] | None = None,
    atomic: bool = True,
) -> Path:
    """Capture and save portable state from an agent-like object."""
    return write_agent_state_snapshot(
        path,
        capture_agent_state(agent),
        metadata=metadata,
        atomic=atomic,
    )


def load_agent_state(
    path: str | os.PathLike[str],
    *,
    strict: bool = True,
) -> AgentState:
    """Load portable agent state from a durable snapshot file."""
    return read_agent_state_snapshot(path, strict=strict).state


def load_agent_state_snapshot(
    path: str | os.PathLike[str],
    *,
    strict: bool = True,
) -> AgentStateSnapshot:
    """Load a durable snapshot including metadata and state."""
    return read_agent_state_snapshot(path, strict=strict)


def restore_agent_state(
    agent: object,
    path: str | os.PathLike[str],
    *,
    strict: bool = True,
    available_skills: list[SkillSpec] | None = None,
) -> AgentState:
    """Load portable state from a snapshot and hydrate an agent-like object."""
    state = load_agent_state(path, strict=strict)
    hydrate_agent_state(
        agent,
        state,
        available_skills=available_skills,
    )
    return state
