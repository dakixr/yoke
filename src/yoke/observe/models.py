"""Typed event and projection models for Yoke Observe."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from typing import Literal

from pydantic import BaseModel
from pydantic import Field


ObserveEventType = Literal[
    "workflow_started",
    "workflow_completed",
    "workflow_failed",
    "node_started",
    "node_completed",
    "node_failed",
    "edge_created",
    "agent_event",
    "typed_output_created",
    "artifact_created",
]

NodeStatus = Literal["pending", "running", "completed", "failed"]
WorkflowStatus = Literal["running", "completed", "failed"]


class ObserveEvent(BaseModel):
    """One immutable workflow observation event."""

    run_id: str
    sequence: int
    type: ObserveEventType
    event_id: str
    timestamp: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    node_id: str | None = None
    parent_node_id: str | None = None
    payload: dict[str, object] = Field(default_factory=dict)


class RunManifest(BaseModel):
    """Durable metadata for one observed workflow run."""

    run_id: str
    name: str
    status: WorkflowStatus = "running"
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    event_count: int = 0


class NodeState(BaseModel):
    """Projected state for one workflow graph node."""

    node_id: str
    label: str
    kind: str
    status: NodeStatus = "pending"
    parent_node_id: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    failed_at: str | None = None
    output_type: str | None = None
    output_preview: object | None = None
    output_schema: object | None = None
    metadata: dict[str, object] = Field(default_factory=dict)
    error: str | None = None
    events: list[dict[str, object]] = Field(default_factory=list)


class EdgeState(BaseModel):
    """Projected dependency edge between two observed nodes."""

    from_node_id: str
    to_node_id: str
    label: str | None = None


class ArtifactState(BaseModel):
    """Projected artifact reference."""

    artifact_id: str
    node_id: str | None = None
    kind: str
    path: str
    media_type: str


class WorkflowState(BaseModel):
    """Current materialized state for an observed workflow run."""

    run_id: str
    name: str | None = None
    status: WorkflowStatus = "running"
    updated_at: str | None = None
    nodes: dict[str, NodeState] = Field(default_factory=dict)
    edges: list[EdgeState] = Field(default_factory=list)
    artifacts: list[ArtifactState] = Field(default_factory=list)
    event_count: int = 0
