"""Schema, registry, path, and mutation contract tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

from yoke.mcp_server.config import MAX_SAFE_REMOTE_WAIT_MS
from yoke.mcp_server.config import MCPServerConfig
from yoke.mcp_server.registry import TOOL_REGISTRY
from yoke.mcp_server.server import create_service

from .helpers import memory_client
from .helpers import structured


EXPECTED_TOOLS = [
    "read_file",
    "view_image",
    "rg",
    "fd",
    "skill",
    "apply_patch",
    "exec_command",
    "exec_python",
    "process_io",
    "mcp_inspect",
    "mcp_call",
    "batch_read",
    "result_read",
    "process_read",
    "process_cancel",
    "search_then_read",
    "workspace_snapshot",
    "check_patch",
    "import_files",
    "write_binary_file",
    "export_file",
]


def test_registry_is_an_explicit_tool_allowlist(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with memory_client(service) as client:
            result = await client.list_tools()
            assert [tool.name for tool in result.tools] == EXPECTED_TOOLS
            assert set(TOOL_REGISTRY) == set(EXPECTED_TOOLS[:9])
            tools = {tool.name: tool for tool in result.tools}
            read_tool = tools["read_file"]
            image_tool = tools["view_image"]
            skill_tool = tools["skill"]
            patch_tool = tools["apply_patch"]
            assert read_tool.annotations is not None
            assert read_tool.annotations.read_only_hint is True
            assert image_tool.annotations is not None
            assert image_tool.annotations.read_only_hint is True
            assert image_tool.annotations.destructive_hint is False
            assert image_tool.annotations.open_world_hint is False
            assert set(image_tool.input_schema["properties"]) == {"path"}
            assert image_tool.input_schema["required"] == ["path"]
            assert (
                tools["exec_command"].input_schema["properties"]["yield_time_ms"][
                    "maximum"
                ]
                == MAX_SAFE_REMOTE_WAIT_MS
            )
            assert (
                tools["process_io"].input_schema["properties"]["yield_time_ms"][
                    "maximum"
                ]
                == MAX_SAFE_REMOTE_WAIT_MS
            )
            assert (
                tools["exec_python"].input_schema["properties"]["yield_time_ms"][
                    "maximum"
                ]
                == MAX_SAFE_REMOTE_WAIT_MS
            )
            assert (
                tools["process_read"].input_schema["properties"]["wait_ms"]["maximum"]
                == MAX_SAFE_REMOTE_WAIT_MS
            )
            assert skill_tool.annotations is not None
            assert skill_tool.annotations.read_only_hint is True
            assert patch_tool.annotations is not None
            assert patch_tool.annotations.destructive_hint is True
            assert read_tool.input_schema == TOOL_REGISTRY[
                "read_file"
            ].tool_class.model_json_schema(by_alias=True)
            assert image_tool.input_schema == TOOL_REGISTRY[
                "view_image"
            ].tool_class.model_json_schema(by_alias=True)

    asyncio.run(scenario())


def test_downstream_gateway_has_conservative_annotations(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with memory_client(service) as client:
            result = await client.list_tools()
            tools = {tool.name: tool for tool in result.tools}
            assert tools["mcp_inspect"].annotations is not None
            assert tools["mcp_inspect"].annotations.read_only_hint is True
            assert tools["mcp_call"].annotations is not None
            assert tools["mcp_call"].annotations.read_only_hint is False
            assert tools["mcp_call"].annotations.destructive_hint is True
            assert tools["mcp_call"].annotations.open_world_hint is True

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
                    "rg", {"raw_args": "needle", "root_dir": str(tmp_path)}
                )
            )
            found = structured(
                await client.call_tool(
                    "fd", {"raw_args": "--glob '*.txt'", "root_dir": str(tmp_path)}
                )
            )
            assert relative["content"].startswith("needle inside")
            assert outside["content"].startswith("needle outside")
            assert absolute["content"].startswith("needle outside")
            assert search["ok"] is True
            assert len(search["output"]) == 2
            assert found["ok"] is True
            assert len(found["output"]) == 2

    asyncio.run(scenario())


def test_rg_and_fd_reject_native_subprocess_switches(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with memory_client(service) as client:
            rg_result = await client.call_tool(
                "rg", {"raw_args": "--pre 'touch should-not-exist' needle"}
            )
            fd_result = await client.call_tool(
                "fd", {"raw_args": "--exec touch should-not-exist"}
            )
            short_fd_result = await client.call_tool(
                "fd", {"raw_args": "-x touch should-not-exist"}
            )
            assert rg_result.is_error is True
            assert fd_result.is_error is True
            assert short_fd_result.is_error is True
            assert not (tmp_path / "should-not-exist").exists()

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
