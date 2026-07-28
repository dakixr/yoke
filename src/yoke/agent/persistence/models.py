"""Durable agent state persistence models."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from typing import Any
from typing import Literal

from pydantic import BaseModel
from pydantic import Field

from yoke._version import __version__
from yoke.agent.state import AgentState

AGENT_STATE_FORMAT = "yoke.agent_state"
AGENT_STATE_SCHEMA_VERSION = 1


def utc_timestamp() -> str:
    """Return an ISO 8601 UTC timestamp."""
    return datetime.now(UTC).isoformat()


class AgentStateSnapshot(BaseModel):
    """Versioned durable snapshot of portable agent state."""

    format: Literal["yoke.agent_state"] = AGENT_STATE_FORMAT
    schema_version: int = AGENT_STATE_SCHEMA_VERSION
    sdk_version: str = __version__
    created_at: str = Field(default_factory=utc_timestamp)
    updated_at: str = Field(default_factory=utc_timestamp)
    metadata: dict[str, Any] = Field(default_factory=dict)
    state: AgentState


class AgentStatePersistenceError(ValueError):
    """Base error for durable agent state persistence failures."""


class AgentStateLoadError(AgentStatePersistenceError):
    """Raised when a durable agent state snapshot cannot be loaded."""


class AgentStateSaveError(AgentStatePersistenceError):
    """Raised when a durable agent state snapshot cannot be saved."""
