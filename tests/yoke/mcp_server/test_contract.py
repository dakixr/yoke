"""Schema, registry, path, and mutation contract tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

from yoke.mcp_server.config import MCPServerConfig
from yoke.mcp_server.registry import TOOL_REGISTRY
from yoke.mcp_server.server import create_service

from .helpers import memory_client
from .helpers import structured


EXPECTED_TOOLS = [
    "read_file",
    "list_files",
    "search_text",
    "find_files",
    "apply_patch",
    "exec_command",
    "exec_python",
    "process_io",
]


def test_registry_is_an_explicit_eight_tool_allowlist(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with memory_client(service) as client:
            result = await client.list_tools()
            assert [tool.name for tool in result.tools] == EXPECTED_TOOLS
            assert set(TOOL_REGISTRY) == set(EXPECTED_TOOLS)
            read_tool = result.tools[0]
            patch_tool = result.tools[4]
            assert read_tool.annotations is not None
            assert read_tool.annotations.read_only_hint is True
            assert patch_tool.annotations is not None
            assert patch_tool.annotations.destructive_hint is True
            assert read_tool.input_schema == TOOL_REGISTRY[
                "read_file"
            ].tool_class.model_json_schema(by_alias=True)

    asyncio.run(scenario())


def test_invalid_arguments_are_recoverable_tool_errors(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with memory_client(service) as client:
            result = await client.call_tool("read_file", {})
            payload = structured(result)
            assert result.is_error is True
            assert payload["ok"] is False
            assert "Invalid tool arguments" in payload["error"]

    asyncio.run(scenario())


def test_file_tools_keep_yoke_path_semantics(tmp_path: Path) -> None:
    root = tmp_path / "root"
    sibling = tmp_path / "sibling.txt"
    root.mkdir()
    (root / "inside.txt").write_text("needle inside\n", encoding="utf-8")
    sibling.write_text("needle outside\n", encoding="utf-8")

    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=root))
        async with memory_client(service) as client:
            relative = structured(
                await client.call_tool("read_file", {"path": "inside.txt"})
            )
            outside = structured(
                await client.call_tool("read_file", {"path": "../sibling.txt"})
            )
            absolute = structured(
                await client.call_tool("read_file", {"path": str(sibling)})
            )
            search = structured(
                await client.call_tool(
                    "search_text", {"query": "needle", "path": str(tmp_path)}
                )
            )
            found = structured(
                await client.call_tool(
                    "find_files", {"pattern": "*.txt", "path": str(tmp_path)}
                )
            )
            assert relative["content"].startswith("needle inside")
            assert outside["content"].startswith("needle outside")
            assert absolute["content"].startswith("needle outside")
            assert search["ok"] is True
            assert search["match_count"] == 2
            assert found["ok"] is True
            assert len(found["matches"]) == 2

    asyncio.run(scenario())


def test_apply_patch_can_create_update_and_delete(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with memory_client(service) as client:
            create = """*** Begin Patch
*** Add File: example.txt
+first
*** End Patch"""
            update = """*** Begin Patch
*** Update File: example.txt
@@
-first
+second
*** End Patch"""
            delete = """*** Begin Patch
*** Delete File: example.txt
*** End Patch"""
            assert structured(await client.call_tool("apply_patch", {"input": create}))[
                "ok"
            ]
            assert (tmp_path / "example.txt").read_text() == "first\n"
            assert structured(await client.call_tool("apply_patch", {"input": update}))[
                "ok"
            ]
            assert (tmp_path / "example.txt").read_text() == "second\n"
            assert structured(await client.call_tool("apply_patch", {"input": delete}))[
                "ok"
            ]
            assert not (tmp_path / "example.txt").exists()

    asyncio.run(scenario())
