"""Full schemas, stale catalogs, shared-client composition and queue isolation."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
import time

import pytest

from yoke.mcp.client import McpToolInfo
from yoke.mcp.config import McpConfig, McpServerConfig
from yoke.mcp.manager import McpManager
from yoke.mcp_server.config import MCPServerConfig
from yoke.mcp_server.execution.gateway import inspect, schema_hash
from yoke.mcp_server.execution.models import Inspect
from yoke.mcp_server.server import create_service

from .helpers import memory_client, structured
from .test_gateway import _write_config, _write_fake_server


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


def test_downstream_bridge_uses_shared_client_and_exact_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    script = tmp_path / "fake.py"
    _write_fake_server(script)
    _write_config(root, script)

    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=root))
        async with memory_client(service) as client:
            discovered = structured(
                await client.call_tool(
                    "mcp_inspect",
                    {"server": "fake", "tools": ["echo"], "include_schemas": True},
                )
            )
            digest = discovered["servers"][0]["tools"][0]["schema_hash"]
            original_client = service.downstream_manager._clients["fake"]
            manifest = [
                {
                    "server": "fake",
                    "tool": "echo",
                    "arguments": {"value": v},
                    "schema_hash": digest,
                }
                for v in ("first", "first-derived")
            ]
            code = f"""
import asyncio
from yoke_mcp import tools, output
async def main():
    first = await tools.mcp('fake', 'echo', {{'value': 'first'}}, schema_hash={digest!r})
    second = await tools.mcp('fake', 'echo', {{'value': first.data['structuredContent']['value'] + '-derived'}}, schema_hash={digest!r})
    output.emit(second.data['structuredContent'])
asyncio.run(main())
"""
            result = structured(
                await client.call_tool(
                    "exec_python",
                    {"code": code, "managed_calls": manifest, "yield_time_ms": 10000},
                )
            )
            assert result["ok"], result
            assert "first-derived" in result["output"]
            assert service.downstream_manager._clients["fake"] is original_client
            denied = structured(
                await client.call_tool(
                    "exec_python",
                    {
                        "code": code,
                        "managed_calls": manifest[:1],
                        "yield_time_ms": 10000,
                    },
                )
            )
            assert not denied["ok"]
            assert "requires an exact outer" in denied["output"]
            stale = await client.call_tool(
                "mcp_call",
                {
                    "server": "fake",
                    "tool": "echo",
                    "arguments": {"value": "x"},
                    "schema_hash": "stale",
                },
            )
            assert stale.is_error

    asyncio.run(scenario())


def test_configured_wrapper_is_schema_pinned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    script = tmp_path / "fake.py"
    _write_fake_server(script)
    _write_config(tmp_path, script)
    schema = {
        "type": "object",
        "properties": {"value": {"type": "string"}},
        "required": ["value"],
    }
    config = tmp_path / "wrappers.json"
    config.write_text(
        json.dumps(
            [
                {
                    "name": "downstream_echo",
                    "server": "fake",
                    "tool": "echo",
                    "description": "Read an echo",
                    "input_schema": schema,
                    "read_only": True,
                }
            ]
        )
    )

    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path, wrappers_file=config))
        async with memory_client(service) as client:
            tool = next(
                t
                for t in (await client.list_tools()).tools
                if t.name == "downstream_echo"
            )
            assert tool.input_schema == schema
            result = structured(
                await client.call_tool(tool.name, {"value": "selected"})
            )
            assert result["structuredContent"]["value"] == "selected"
            code = f"import asyncio\nfrom yoke_mcp import tools, output\noutput.emit(asyncio.run(tools.mcp('fake', 'echo', {{'value': 'read'}}, schema_hash={schema_hash(schema)!r})).data)"
            composed = structured(
                await client.call_tool(
                    "exec_python", {"code": code, "yield_time_ms": 10000}
                )
            )
            assert composed["ok"], composed

    asyncio.run(scenario())
