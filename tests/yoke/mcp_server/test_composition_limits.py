"""Cancellation and budget boundaries that need more than happy-path calls."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import time

import pytest

from yoke.mcp.client import McpToolInfo
from yoke.mcp.config import McpConfig, McpServerConfig
from yoke.mcp.manager import McpManager
from yoke.mcp_server.config import MCPServerConfig
from yoke.mcp_server.execution.gateway import call
from yoke.mcp_server.execution.models import BatchRead
from yoke.mcp_server.execution.processes import ProcessCursor, ProcessRead, page
from yoke.mcp_server.results.store import ResultStore
from yoke.mcp_server.server import create_service

from .helpers import memory_client, structured


def test_batch_deadline_cancels_children_and_preserves_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        started = []
        stopped = []

        async def slow(name, arguments, *, cancel=None):
            started.append(arguments["path"])
            try:
                await asyncio.sleep(30)
            finally:
                stopped.append(arguments["path"])

        monkeypatch.setattr(service.adapter.execution, "local", slow)
        request = BatchRead.model_validate(
            {
                "deadline_ms": 20,
                "max_concurrency": 1,
                "items": [
                    {"id": str(i), "tool": "read_file", "arguments": {"path": str(i)}}
                    for i in range(3)
                ],
            }
        )
        before = time.monotonic()
        result = await service.adapter.execution.batch(request)
        assert time.monotonic() - before < 1
        assert [r["id"] for r in result["items"]] == ["0", "1", "2"]
        assert all(r["status"] == "cancelled" for r in result["items"])
        assert started == stopped == ["0"]
        await service.adapter.execution.close()
        await service.runtime.close()

    asyncio.run(scenario())


def test_process_cursor_pages_large_unicode_output_without_duplicates(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with memory_client(service) as client:
            text = "界é" * 20000
            running = structured(
                await client.call_tool(
                    "exec_python",
                    {
                        "code": f"import time; print({text!r}, flush=True); time.sleep(30)",
                        "yield_time_ms": 250,
                    },
                )
            )
            session = running["session_id"]
            await service.adapter.execution.dispatch(
                "process_read", {"sessions": [{"session_id": session}], "wait_ms": 5000}
            )
            cursor = ProcessCursor(session_id=session)
            collected = ""
            for _ in range(200):
                observed = page(service.runtime.manager, cursor, 1024)
                assert not observed["gap"]
                collected += observed["output"]
                cursor = ProcessCursor.model_validate(observed["next_cursor"])
                if collected.endswith("\n"):
                    break
                if not observed["output"]:
                    await asyncio.sleep(0.02)
            assert collected == text + "\n"
            assert page(service.runtime.manager, cursor, 1024)["output"] == ""
            await client.call_tool("process_cancel", {"session_id": session})

    asyncio.run(scenario())


def test_projection_enforces_wire_budget_after_json_escaping() -> None:
    store = ResultStore()
    result = store.project({"ok": True, "content": '\\"界\n' * 10000}, limit=1024)
    assert len(json.dumps(result, ensure_ascii=True)) <= 1024
    assert result["truncated"]


def test_uncertain_downstream_write_is_not_retried(tmp_path: Path) -> None:
    class Client:
        server_instructions: str | None = None
        calls = 0

        def list_tools(self, *, force=False):
            return (McpToolInfo("write", "", {"type": "object"}),)

        def call_tool(self, name, arguments, *, cancel_requested=None):
            self.calls += 1
            raise TimeoutError("response lost")

        def close(self):
            pass

    client = Client()
    manager = McpManager(
        McpConfig((McpServerConfig(name="fake", command="unused"),)), root=tmp_path
    )
    manager._clients["fake"] = client
    result = call(manager, server="fake", tool="write", arguments={})
    assert result["status"] == "unknown"
    assert result["retry"] == "inspect_outcome_before_retry"
    assert client.calls == 1


def test_process_read_limit_is_explicit() -> None:
    with pytest.raises(ValueError):
        ProcessRead.model_validate({"sessions": [{"session_id": 1}] * 17})
