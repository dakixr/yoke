"""End-to-end composed reads, subprocess IPC and workflow contracts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from pathlib import Path

from yoke.mcp_server.config import MCPServerConfig
from yoke.mcp_server.server import create_service

from .helpers import memory_client, structured


def test_batch_partial_failures_and_retained_results(tmp_path: Path) -> None:
    (tmp_path / "large").write_text("needle " * 10000)
    (tmp_path / "small").write_text("hello")

    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with memory_client(service) as client:
            result = structured(
                await client.call_tool(
                    "batch_read",
                    {
                        "items": [
                            {"id": p, "tool": "read_file", "arguments": {"path": p}}
                            for p in ("large", "missing", "small")
                        ],
                        "max_output_tokens": 1024,
                    },
                )
            )
            assert result["ok"]
            assert [r["id"] for r in result["items"]] == ["large", "missing", "small"]
            assert [r["status"] for r in result["items"]] == ["ok", "error", "ok"]
            assert result["items"][2]["data"]["content"] == "hello"
            assert len(json.dumps(result)) <= 4096
            ref = result["items"][0]["data"]["result_ref"]
            first = structured(
                await client.call_tool("result_read", {"result_ref": ref, "limit": 100})
            )
            again = structured(
                await client.call_tool("result_read", {"result_ref": ref, "limit": 100})
            )
            assert first == again
            assert first["next_cursor"] == 100
            forbidden = await client.call_tool(
                "batch_read",
                {
                    "items": [
                        {
                            "id": "x",
                            "tool": "exec_command",
                            "arguments": {"cmd": "touch forbidden"},
                        }
                    ]
                },
            )
            assert forbidden.is_error
            assert not (tmp_path / "forbidden").exists()
            duplicate = await client.call_tool(
                "batch_read",
                {
                    "items": [
                        {"id": "x", "tool": "read_file", "arguments": {"path": "small"}}
                    ]
                    * 2
                },
            )
            assert duplicate.is_error

    asyncio.run(scenario())


def test_python_bridge_dependent_reads_with_one_operation_slot(tmp_path: Path) -> None:
    (tmp_path / "index").write_text("target")
    (tmp_path / "target").write_text("selected evidence")

    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path, max_concurrent_calls=1))
        async with memory_client(service) as client:
            result = structured(
                await client.call_tool(
                    "exec_python",
                    {
                        "code": """
import asyncio
from yoke_mcp import tools, output
async def main():
    index = await tools.call('read_file', {'path': 'index'})
    result = await tools.call('read_file', {'path': index.data['content']})
    output.emit({'evidence': result.data['content']})
asyncio.run(main())
""",
                        "yield_time_ms": 5000,
                    },
                )
            )
            assert result["ok"], result
            assert "selected evidence" in result["output"]
            emitted = json.loads(result["output"])
            retained = structured(
                await client.call_tool(
                    "result_read", {"result_ref": emitted["result_ref"]}
                )
            )
            assert json.loads(retained["content"]) == {"evidence": "selected evidence"}
            assert not service.adapter.execution.bridge._runs

    asyncio.run(scenario())


def test_python_bridge_rejects_unlisted_effects(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with memory_client(service) as client:
            result = structured(
                await client.call_tool(
                    "exec_python",
                    {
                        "code": """
import asyncio
from yoke_mcp import tools
asyncio.run(tools.call('exec_command', {'cmd': 'touch forbidden'}))
""",
                        "yield_time_ms": 5000,
                    },
                )
            )
            assert not result["ok"]
            assert "Only local read tools" in result["output"]
            assert not (tmp_path / "forbidden").exists()

    asyncio.run(scenario())


def test_process_read_replays_cursors_and_cancel(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with memory_client(service) as client:
            running = structured(
                await client.call_tool(
                    "exec_python",
                    {
                        "code": "import time; print('hello', flush=True); time.sleep(30)",
                        "yield_time_ms": 250,
                    },
                )
            )
            session = running["session_id"]
            request = {"sessions": [{"session_id": session}]}
            first = structured(
                await client.call_tool("process_read", {**request, "wait_ms": 5000})
            )
            again = structured(await client.call_tool("process_read", request))
            assert first["items"][0]["output"] == "hello\n"
            assert again["items"][0]["output"] == "hello\n"
            assert first["items"][0]["next_cursor"] == again["items"][0]["next_cursor"]
            assert first["items"][0]["continue"] is True
            assert first["items"][0]["next_tool"] == "process_read"
            assert first["items"][0]["recommended_wait_ms"] == 240_000
            assert first["items"][0]["elapsed_seconds"] > 0
            next_page = structured(
                await client.call_tool(
                    "process_read", {"sessions": [first["items"][0]["next_cursor"]]}
                )
            )
            assert next_page["items"][0]["output"] == ""
            cancelled = structured(
                await client.call_tool("process_cancel", {"session_id": session})
            )
            assert cancelled["ok"]
            assert service.runtime.manager.snapshot(session).status != "running"
            assert not service.adapter.execution.bridge._runs

    asyncio.run(scenario())


def test_search_recipe_and_hash_guarded_patch(tmp_path: Path) -> None:
    path = tmp_path / "example.txt"
    path.write_text("before\nneedle\nafter\n")
    patch = "*** Begin Patch\n*** Update File: example.txt\n@@\n-before\n+changed\n*** End Patch"

    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with memory_client(service) as client:
            search = structured(
                await client.call_tool("search_then_read", {"pattern": "needle"})
            )
            assert search["reads"] and "needle" in search["reads"][0]["content"]
            request = {
                "input": patch,
                "expected_hashes": {"example.txt": "wrong"},
                "checks": [
                    {
                        "name": "first",
                        "argv": [sys.executable, "-c", "print('check one')"],
                    },
                    {
                        "name": "second",
                        "argv": [sys.executable, "-c", "print('check two')"],
                    },
                ],
            }
            skipped = structured(await client.call_tool("check_patch", request))
            assert not skipped["ok"] and path.read_text().startswith("before")
            request["expected_hashes"] = {
                "example.txt": hashlib.sha256(path.read_bytes()).hexdigest()
            }
            result = structured(await client.call_tool("check_patch", request))
            assert result["ok"], result
            assert path.read_text().startswith("changed")
            output = result["execution"]["output"]
            if result["execution"].get("session_id"):
                cursor = {"session_id": result["execution"]["session_id"]}
                for _ in range(20):
                    observed = structured(
                        await client.call_tool(
                            "process_read", {"sessions": [cursor], "wait_ms": 1000}
                        )
                    )["items"][0]
                    output += observed["output"]
                    cursor = observed["next_cursor"]
                    if observed["status"] != "running":
                        break
            assert "check one" in output
            assert "check two" in output
            assert '"diff"' in output

    asyncio.run(scenario())


def test_descriptors_defaults_file_parameters_and_output_schemas(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path, default_yield_ms=1234))
        async with memory_client(service) as client:
            tools = {t.name: t for t in (await client.list_tools()).tools}
            assert (
                tools["exec_command"].input_schema["properties"]["login"]["default"]
                is False
            )
            assert (
                tools["exec_python"].input_schema["properties"]["yield_time_ms"][
                    "default"
                ]
                == 1234
            )
            assert tools["import_files"].meta == {"openai/fileParams": ["files"]}
            assert tools["batch_read"].output_schema
            assert tools["process_read"].annotations is not None
            assert tools["exec_python"].annotations is not None
            assert tools["process_read"].annotations.read_only_hint
            assert not tools["exec_python"].annotations.read_only_hint

    asyncio.run(scenario())
