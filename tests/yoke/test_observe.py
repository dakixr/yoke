from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import cast

from pydantic import BaseModel

from yoke.ai import Agent
from yoke.ai import RunConfig
from yoke.agent.models import Message
from yoke.ai.providers.base import Provider
from yoke.cli.main import main
from yoke.cli.observe_app import _handler_for_store
from yoke.observe import JsonlObserveStore
from yoke.observe import step
from yoke.observe import workflow


class Summary(BaseModel):
    verdict: str
    risks: list[str]


class Decision(BaseModel):
    accepted: bool


class Batch(BaseModel):
    items: list[Summary]


class StaticProvider(Provider):
    def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
    ) -> Message:
        del messages, tools
        return Message.assistant('{"verdict":"pass","risks":[]}')


def test_observe_records_sdk_typed_output(tmp_path: Path) -> None:
    agent = Agent(
        provider=StaticProvider(),
        config=RunConfig(root=tmp_path, include_agents_file=False),
    )

    with workflow("review", root=tmp_path) as run:
        result = agent.prompt("Review.", output_type=Summary)

    assert result.structured == Summary(verdict="pass", risks=[])
    store = JsonlObserveStore(tmp_path)
    events = list(store.events(run.run_id))
    state = store.latest_state(run.run_id)

    assert [event.type for event in events] == [
        "workflow_started",
        "node_started",
        "agent_event",
        "agent_event",
        "agent_event",
        "agent_event",
        "typed_output_created",
        "agent_event",
        "node_completed",
        "workflow_completed",
    ]
    assert state is not None
    assert state.status == "completed"
    assert len(state.nodes) == 1
    node = next(iter(state.nodes.values()))
    assert node.kind == "agent"
    assert node.output_type == "Summary"
    assert node.output_preview == {"verdict": "pass", "risks": []}
    assert node.output_schema is not None
    agent_metadata = cast(dict[str, object], node.metadata["agent"])
    assert isinstance(agent_metadata, dict)
    assert agent_metadata["prompt"] == "Review."
    assert agent_metadata["output_type"] == "Summary"
    assert [
        event.payload.get("event") for event in events if event.type == "agent_event"
    ] == [
        "prompt_started",
        "model_start",
        "model_end",
        "iteration_end",
        "prompt_completed",
    ]


def test_observe_reuses_agent_node_for_stateful_conversation(
    tmp_path: Path,
) -> None:
    agent = Agent(
        provider=StaticProvider(),
        config=RunConfig(root=tmp_path, include_agents_file=False),
    )

    with workflow("conversation", root=tmp_path) as run:
        first = agent.prompt("Draft.", output_type=Summary)
        second = agent.prompt("Review the draft.", output_type=Summary)

    assert first.structured == Summary(verdict="pass", risks=[])
    assert second.structured == Summary(verdict="pass", risks=[])
    state = JsonlObserveStore(tmp_path).latest_state(run.run_id)

    assert state is not None
    assert len(state.nodes) == 1
    node = next(iter(state.nodes.values()))
    assert node.kind == "agent"
    assert node.status == "completed"
    prompt_events = [
        event for event in node.events if event.get("event") == "prompt_started"
    ]
    assert len(prompt_events) == 2
    assert prompt_events[0]["prompt"] == "Draft."
    assert prompt_events[1]["prompt"] == "Review the draft."


def test_observe_step_infers_edges_from_pydantic_values(tmp_path: Path) -> None:
    @step
    def produce() -> Summary:
        return Summary(verdict="pass", risks=[])

    @step
    def consume(summary: Summary) -> Decision:
        return Decision(accepted=summary.verdict == "pass")

    with workflow("steps", root=tmp_path) as run:
        summary = produce()
        consume(summary)

    state = JsonlObserveStore(tmp_path).latest_state(run.run_id)

    assert state is not None
    assert len(state.nodes) == 2
    assert len(state.edges) == 1
    producer = next(node for node in state.nodes.values() if node.label == "produce")
    consumer = next(node for node in state.nodes.values() if node.label == "consume")
    assert state.edges[0].from_node_id == producer.node_id
    assert state.edges[0].to_node_id == consumer.node_id
    assert producer.output_type == "Summary"
    assert consumer.output_type == "Decision"
    assert producer.metadata["inputs"] == {"args": [], "kwargs": {}}
    source = cast(dict[str, object], producer.metadata["source"])
    assert isinstance(source, dict)
    assert source["name"] == "produce"
    assert "def produce" in str(source["code"])


def test_observe_step_infers_edges_from_nested_pydantic_values(
    tmp_path: Path,
) -> None:
    @step
    def produce() -> Batch:
        return Batch(items=[Summary(verdict="pass", risks=[])])

    @step
    def consume(summary: Summary) -> Decision:
        return Decision(accepted=summary.verdict == "pass")

    with workflow("nested", root=tmp_path) as run:
        batch = produce()
        consume(batch.items[0])

    state = JsonlObserveStore(tmp_path).latest_state(run.run_id)

    assert state is not None
    producer = next(node for node in state.nodes.values() if node.label == "produce")
    consumer = next(node for node in state.nodes.values() if node.label == "consume")
    assert producer.output_preview == {"items": [{"verdict": "pass", "risks": []}]}
    assert any(
        edge.from_node_id == producer.node_id
        and edge.to_node_id == consumer.node_id
        and edge.label == "input"
        for edge in state.edges
    )


def test_observe_typed_output_preview_preserves_large_object_shape(
    tmp_path: Path,
) -> None:
    @step
    def produce() -> Batch:
        return Batch(items=[Summary(verdict="x" * 2000, risks=["risk"])])

    with workflow("preview", root=tmp_path) as run:
        produce()

    state = JsonlObserveStore(tmp_path).latest_state(run.run_id)

    assert state is not None
    node = next(iter(state.nodes.values()))
    assert node.output_preview == {
        "items": [
            {
                "verdict": {
                    "__truncated__": True,
                    "characters": 2000,
                    "preview": "x" * 1200,
                },
                "risks": ["risk"],
            }
        ]
    }


def test_observe_projects_step_to_agent_containment_edge(tmp_path: Path) -> None:
    agent = Agent(
        provider=StaticProvider(),
        config=RunConfig(root=tmp_path, include_agents_file=False),
    )

    @step
    def review() -> Summary:
        result = agent.prompt("Review.", output_type=Summary)
        assert result.structured is not None
        return result.structured

    with workflow("contains", root=tmp_path) as run:
        review()

    state = JsonlObserveStore(tmp_path).latest_state(run.run_id)

    assert state is not None
    step_node = next(node for node in state.nodes.values() if node.kind == "step")
    agent_node = next(node for node in state.nodes.values() if node.kind == "agent")
    assert agent_node.parent_node_id == step_node.node_id
    assert any(
        edge.from_node_id == step_node.node_id
        and edge.to_node_id == agent_node.node_id
        and edge.label == "contains"
        for edge in state.edges
    )


def test_observe_cli_state_outputs_projection(
    tmp_path: Path,
    capsys,
) -> None:
    with workflow("cli", root=tmp_path) as run:
        pass

    exit_code = main(
        ["observe", "state", run.run_id, "--root", str(tmp_path), "--json"]
    )
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert exit_code == 0
    assert payload["run_id"] == run.run_id
    assert payload["name"] == "cli"
    assert payload["status"] == "completed"


def test_observe_server_serves_ui_and_json_state(tmp_path: Path) -> None:
    with workflow("web", root=tmp_path) as run:
        pass

    store = JsonlObserveStore(tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler_for_store(store))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_port
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as response:
            html = response.read().decode("utf-8")
            content_type = response.headers["Content-Type"]
            cache_control = response.headers["Cache-Control"]
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/runs/{run.run_id}", timeout=5
        ) as response:
            run_html = response.read().decode("utf-8")
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/runs/{run.run_id}/state", timeout=5
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert "text/html" in content_type
    assert cache_control == "no-store"
    assert "Yoke Observe" in html
    assert "const state" in html
    assert "cytoscape-dagre" in html
    assert 'rankDir: "TB"' in html
    assert "Input" in html
    assert "Output" in html
    assert "graphElements" in html
    assert "child_agents" in html
    assert "Open details" in html
    assert "detailOverlay" in html
    assert "detail-nav" in html
    assert "detail-workbench" in html
    assert "Agent Activity" in html
    assert "<h3>Metadata</h3>" not in html
    assert "Yoke Observe" in run_html
    assert payload["run_id"] == run.run_id
