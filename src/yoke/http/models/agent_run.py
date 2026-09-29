"""Bounded public agent-run observations shared by HTTP and its ACP client."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Short = Annotated[str, Field(max_length=256)]
Identity = Annotated[str, Field(min_length=1, max_length=256)]
Timestamp = Annotated[str, Field(min_length=1, max_length=64)]
Count = Annotated[int, Field(ge=0)]


class AgentUsage(BaseModel):
    """Cumulative counters, never arbitrary provider metadata."""

    model_config = ConfigDict(extra="ignore", strict=True, allow_inf_nan=False)
    totalTokens: Count | None = None
    inputTokens: Count | None = None
    outputTokens: Count | None = None
    cachedInputTokens: Count | None = None
    reasoningOutputTokens: Count | None = None
    toolUses: Count | None = None
    durationMs: Annotated[float, Field(ge=0)] | None = None


class AgentRunSnapshot(BaseModel):
    """The registry's wire names are deliberately independent of API aliases."""

    model_config = ConfigDict(extra="ignore", strict=True)
    schemaVersion: Literal[1]
    agentId: Identity
    runId: Identity
    sessionID: Identity | None
    parentRunId: Identity | None = None
    parentAgentId: Identity | None = None
    runtimeSessionID: Count | None = None
    name: Short
    provider: Short
    model: Short | None = None
    status: Literal["running", "completed", "failed", "cancelled", "interrupted"]
    observation: Literal["live", "stale", "lost"]
    startedAt: Timestamp
    finishedAt: Timestamp | None = None
    lastSeenAt: Timestamp
    updatedAt: Timestamp
    version: Count
    lastToolName: Short | None = None
    errorType: Annotated[Short, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_.]*$")] | None = (
        None
    )
    attempt: Count | None = None
    taskId: Identity | None = None
    originToolCallId: Identity | None = None
    originTurnId: Identity | int | None = None
    effort: Short | None = None
    typedUsage: AgentUsage | None = None


class AgentRunListResponse(BaseModel):
    """Authoritative retained roster for one native session."""

    data: list[AgentRunSnapshot]


def public_snapshot(value: object, session_id: str) -> dict[str, object] | None:
    """Reject foreign ownership and strip non-contract fields before storage."""
    if not isinstance(value, dict) or value.get("sessionID") not in (None, session_id):
        return None
    fields = {**value, "sessionID": session_id}
    error_type = fields.get("errorType")
    if error_type is not None and (
        not isinstance(error_type, str)
        or len(error_type) > 256
        or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", error_type) is None
    ):
        # Python permits Unicode and dynamically assigned exception names.
        # An unsupported optional label must never hide a terminal outcome.
        fields["errorType"] = None
    try:
        parsed = AgentRunSnapshot.model_validate(fields)
    except ValueError:
        return None
    return parsed.model_dump(exclude_unset=True)
