"""File IO for durable agent state snapshots."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from pydantic_core import PydanticSerializationError

from yoke._file_io import atomic_write_text
from yoke.agent.persistence.models import AGENT_STATE_FORMAT
from yoke.agent.persistence.models import AGENT_STATE_SCHEMA_VERSION
from yoke.agent.persistence.models import AgentStateLoadError
from yoke.agent.persistence.models import AgentStateSaveError
from yoke.agent.persistence.models import AgentStateSnapshot
from yoke.agent.persistence.models import utc_timestamp
from yoke.agent.state import AgentState


def read_agent_state_snapshot(
    path: str | os.PathLike[str],
    *,
    strict: bool = True,
) -> AgentStateSnapshot:
    """Read and validate a durable agent state snapshot."""
    resolved = _resolve_path(path)
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AgentStateLoadError(
            f"Failed to read agent state snapshot {resolved}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise AgentStateLoadError("Agent state snapshot must be a JSON object.")
    _validate_envelope(payload, strict=strict)
    try:
        return AgentStateSnapshot.model_validate(payload)
    except ValidationError as exc:
        raise AgentStateLoadError(
            f"Invalid agent state snapshot {resolved}: {exc}"
        ) from exc


def write_agent_state_snapshot(
    path: str | os.PathLike[str],
    state: AgentState,
    *,
    metadata: dict[str, Any] | None = None,
    atomic: bool = True,
) -> Path:
    """Write a durable agent state snapshot and return its path."""
    resolved = _resolve_path(path)
    snapshot = AgentStateSnapshot(
        created_at=utc_timestamp(),
        updated_at=utc_timestamp(),
        metadata=dict(metadata or {}),
        state=state,
    )
    return write_agent_state_snapshot_model(
        resolved,
        snapshot,
        atomic=atomic,
    )


def write_agent_state_snapshot_model(
    path: str | os.PathLike[str],
    snapshot: AgentStateSnapshot,
    *,
    atomic: bool = True,
) -> Path:
    """Write a prebuilt durable agent state snapshot."""
    resolved = _resolve_path(path)
    try:
        data = snapshot.model_dump_json(indent=2) + "\n"
        if atomic:
            atomic_write_text(resolved, data, fsync=True)
        else:
            resolved.parent.mkdir(parents=True, exist_ok=True)
            resolved.write_text(data, encoding="utf-8")
    except (OSError, UnicodeError, PydanticSerializationError) as exc:
        raise AgentStateSaveError(
            f"Failed to write agent state snapshot {resolved}: {exc}"
        ) from exc
    return resolved


def _validate_envelope(payload: dict[str, Any], *, strict: bool) -> None:
    if payload.get("format") != AGENT_STATE_FORMAT:
        raise AgentStateLoadError(
            f"Unsupported agent state format: {payload.get('format')!r}."
        )
    schema_version = payload.get("schema_version")
    if not isinstance(schema_version, int):
        raise AgentStateLoadError("Agent state snapshot missing schema_version.")
    if schema_version > AGENT_STATE_SCHEMA_VERSION:
        qualifier = "" if strict else " yet"
        raise AgentStateLoadError(
            "Agent state snapshot schema_version "
            f"{schema_version} is not supported{qualifier}."
        )
    if schema_version < 1:
        raise AgentStateLoadError(
            f"Unsupported agent state schema_version: {schema_version}."
        )


def _resolve_path(path: str | os.PathLike[str]) -> Path:
    return Path(path).expanduser().resolve()
