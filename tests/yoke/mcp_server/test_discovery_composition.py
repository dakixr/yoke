"""Full schemas, stale catalogs, shared-client composition and queue isolation."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import time

import pytest

from yoke.mcp.client import McpToolInfo
from yoke.mcp.config import McpConfig, McpServerConfig
from yoke.mcp.manager import McpManager
from yoke.mcp_server.execution.gateway import inspect
from yoke.mcp_server.execution.models import Inspect


SCHEMA = {
    "type": "object",
    "$defs": {"value": {"type": "string"}},
    "properties": {"value": {"$ref": "#/$defs/value"}},
    "required": ["value"],
    "anyOf": [{"required": ["value"]}],
}


class CatalogClient:
    server_instructions: str | None = None

    def __init__(self) -> None:
        self.tools = tuple(
            McpToolInfo(f"tool_{i:03}", "description", SCHEMA) for i in range(105)
        )

    def list_tools(self, *, force=False):
        return self.tools

    def call_tool(self, name, arguments, *, cancel_requested=None):
        return {"content": [{"type": "text", "text": "result"}]}

    def close(self):
        pass


def test_complete_schema_pagination_and_stale_cursor(tmp_path: Path) -> None:
    config = McpServerConfig(name="fake", command="unused")
    manager = McpManager(McpConfig((config,)), root=tmp_path)
    client = CatalogClient()
    manager._clients["fake"] = client
    first = inspect(manager, Inspect(include_schemas=True, limit=100))
    assert len(first["servers"][0]["tools"]) == 100
    assert first["servers"][0]["tools"][0]["input_schema"] == SCHEMA
    second = inspect(
        manager, Inspect(include_schemas=True, limit=100, cursor=first["next_cursor"])
    )
    assert len(second["servers"][0]["tools"]) == 5
    assert second["next_cursor"] is None
    client.tools = client.tools[:104]
    with pytest.raises(ValueError, match="Catalog changed"):
        inspect(manager, Inspect(include_schemas=True, cursor=first["next_cursor"]))


def test_queued_server_does_not_block_another_server(tmp_path: Path) -> None:
    manager = McpManager(
        McpConfig(tuple(McpServerConfig(name=n, command="unused") for n in ("a", "b"))),
        root=tmp_path,
    )
    held = manager._server_lock("a")
    held.acquire()
    started = threading.Event()

    def acquire(name):
        if name == "a":
            started.set()
        _, lock, _ = manager._acquire_server(name)
        assert lock is not None
        lock.release()

    with ThreadPoolExecutor(max_workers=2) as pool:
        queued = pool.submit(acquire, "a")
        assert started.wait(1)
        time.sleep(0.02)
        other = pool.submit(acquire, "b")
        try:
            other.result(timeout=1)
        finally:
            held.release()
        queued.result(timeout=1)
