"""Projection reducer for Yoke Observe events."""

from __future__ import annotations

from collections.abc import Iterable

from yoke.observe.models import ArtifactState
from yoke.observe.models import EdgeState
from yoke.observe.models import NodeState
from yoke.observe.models import ObserveEvent
from yoke.observe.models import WorkflowState


def project_events(events: Iterable[ObserveEvent]) -> WorkflowState | None:
    """Build the current workflow state by replaying events."""
    state: WorkflowState | None = None
    for event in events:
        if state is None:
            state = WorkflowState(run_id=event.run_id)
        apply_event(state, event)
    return state


def apply_event(state: WorkflowState, event: ObserveEvent) -> None:
    """Apply one observation event to a mutable projection."""
    state.event_count = max(state.event_count, event.sequence)
    state.updated_at = event.timestamp
    if event.type == "workflow_started":
        name = event.payload.get("name")
        state.name = name if isinstance(name, str) else state.name
        state.status = "running"
        return
    if event.type == "workflow_completed":
        state.status = "completed"
        return
    if event.type == "workflow_failed":
        state.status = "failed"
        return
    if event.type == "node_started" and event.node_id is not None:
        label = event.payload.get("label")
        kind = event.payload.get("kind")
        metadata = {
            key: value
            for key, value in event.payload.items()
            if key not in {"label", "kind"}
        }
        state.nodes[event.node_id] = NodeState(
            node_id=event.node_id,
            label=label if isinstance(label, str) else event.node_id,
            kind=kind if isinstance(kind, str) else "step",
            status="running",
            parent_node_id=event.parent_node_id,
            started_at=event.timestamp,
            metadata=metadata,
        )
        if event.parent_node_id is not None:
            _append_edge(
                state,
                EdgeState(
                    from_node_id=event.parent_node_id,
                    to_node_id=event.node_id,
                    label="contains",
                ),
            )
        return
    if event.type == "node_completed" and event.node_id is not None:
        node = _node_for_event(state, event)
        node.status = "completed"
        node.completed_at = event.timestamp
        return
    if event.type == "node_failed" and event.node_id is not None:
        node = _node_for_event(state, event)
        node.status = "failed"
        node.failed_at = event.timestamp
        error = event.payload.get("error")
        node.error = error if isinstance(error, str) else None
        return
    if event.type == "edge_created":
        from_node = event.payload.get("from_node_id")
        to_node = event.payload.get("to_node_id")
        if isinstance(from_node, str) and isinstance(to_node, str):
            label = event.payload.get("label")
            edge = EdgeState(
                from_node_id=from_node,
                to_node_id=to_node,
                label=label if isinstance(label, str) else None,
            )
            _append_edge(state, edge)
        return
    if event.type == "agent_event" and event.node_id is not None:
        node = _node_for_event(state, event)
        payload = {"timestamp": event.timestamp, **event.payload}
        node.events.append(payload)
        if event.payload.get("event") == "agent_metadata":
            for key in ("agent", "source"):
                value = event.payload.get(key)
                if value is not None:
                    node.metadata[key] = value
        return
    if event.type == "typed_output_created" and event.node_id is not None:
        node = _node_for_event(state, event)
        output_type = event.payload.get("type")
        node.output_type = output_type if isinstance(output_type, str) else None
        node.output_preview = event.payload.get("preview")
        node.output_schema = event.payload.get("schema")
        return
    if event.type == "artifact_created":
        artifact_id = event.payload.get("artifact_id")
        kind = event.payload.get("kind")
        path = event.payload.get("path")
        media_type = event.payload.get("media_type")
        if (
            isinstance(artifact_id, str)
            and isinstance(kind, str)
            and isinstance(path, str)
            and isinstance(media_type, str)
        ):
            state.artifacts.append(
                ArtifactState(
                    artifact_id=artifact_id,
                    node_id=event.node_id,
                    kind=kind,
                    path=path,
                    media_type=media_type,
                )
            )


def _node_for_event(state: WorkflowState, event: ObserveEvent) -> NodeState:
    assert event.node_id is not None
    node = state.nodes.get(event.node_id)
    if node is not None:
        return node
    node = NodeState(
        node_id=event.node_id,
        label=event.node_id,
        kind="unknown",
        status="running",
        parent_node_id=event.parent_node_id,
    )
    state.nodes[event.node_id] = node
    return node


def _append_edge(state: WorkflowState, edge: EdgeState) -> None:
    if edge not in state.edges:
        state.edges.append(edge)
