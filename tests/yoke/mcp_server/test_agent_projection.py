"""Differential protection for the agent's established MCP presentation."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest

from yoke.mcp.client import McpToolInfo
from yoke.mcp.config import McpConfig, McpServerConfig
from yoke.mcp.manager import McpManager

from . import legacy_projection


PAYLOADS = [
    {"content": [{"type": "text", "text": "tool result"}], "isError": False},
    {
        "content": [{"type": "text", "text": "# Skill\nInstructions must stay exact."}],
        "structuredContent": {"skill": "test", "instructions": "unchanged"},
    },
    {
        "structuredContent": {
            "checkpoint": {"branch": "main", "messages": ["system", "user", "tool"]}
        }
    },
    {
        "content": [
            {"type": "image", "mimeType": "image/png", "data": "unchanged"},
            {"type": "audio", "mimeType": "audio/wav", "data": "unchanged"},
        ]
    },
    {
        "content": [
            {
                "type": "resource",
                "resource": {"uri": "checkpoint://branch/2", "text": "preserve order"},
            }
        ]
    },
    {
        "content": [{"type": "text", "text": "branch\n" * 10000}],
        "structuredContent": {"tools": [{"value": "x" * 1000}] * 200},
    },
    {"content": [{"type": "text", "text": "failure"}], "isError": True},
    {
        "ok": False,
        "content": [{"type": "text", "text": "nonstandard downstream field"}],
    },
]


class ProjectionClient:
    server_instructions: str | None = None

    def __init__(self, result: dict[str, Any]) -> None:
        self.result = result

    def list_tools(self, *, force=False):
        return (McpToolInfo("sample", "", {}),)

    def call_tool(self, name, arguments, *, cancel_requested=None):
        return self.result

    def close(self):
        pass


@pytest.mark.parametrize("payload", PAYLOADS)
def test_agent_projection_matches_established_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]
) -> None:
    for module in ("yoke.mcp.results", "tests.yoke.mcp_server.legacy_projection"):
        monkeypatch.setattr(
            module + ".persist_full_mcp_text", lambda *a, **k: "/retained/text"
        )
        monkeypatch.setattr(
            module + ".persist_full_mcp_result", lambda *a, **k: "/retained/json"
        )
    server = McpServerConfig(name="sample", command="unused")
    manager = McpManager(McpConfig((server,)), root=tmp_path)
    manager._clients["sample"] = ProjectionClient(payload)
    expected = legacy_projection.project(payload, server="sample", tool="sample")
    assert manager.call_tool(server="sample", tool="sample", arguments={}) == expected


def test_agent_discovery_keeps_its_existing_projection(tmp_path: Path) -> None:
    from yoke.mcp.config import compact_tool_schema
    from yoke.mcp_server.execution.gateway import inspect
    from yoke.mcp_server.execution.models import Inspect
    from .test_discovery_composition import CatalogClient, SCHEMA

    server = McpServerConfig(name="sample", command="unused")
    manager = McpManager(McpConfig((server,)), root=tmp_path)
    manager._clients["sample"] = CatalogClient()
    before = cast(dict[str, Any], manager.inspect(include_schemas=True))
    enhanced = inspect(manager, Inspect(include_schemas=True))
    assert enhanced["servers"][0]["tools"][0]["input_schema"] == SCHEMA
    assert manager.inspect(include_schemas=True) == before
    assert before["servers"][0]["tools"][0]["input_schema"] == compact_tool_schema(
        SCHEMA, include_schema=True
    )
