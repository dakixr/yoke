"""Regressions for verification launch, transfer cleanup and schema composition."""

from __future__ import annotations

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from yoke.mcp_server.config import MCPServerConfig
from yoke.mcp_server.execution.wrappers import Wrapper
from yoke.mcp_server.server import create_service

from .helpers import memory_client, structured


def test_failed_uploads_release_slots(tmp_path: Path) -> None:
    (tmp_path / "existing").write_text("preserved")

    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with memory_client(service) as client:
            for _ in range(9):
                result = await client.call_tool(
                    "write_binary_file", {"path": "existing", "data_base64": "YQ=="}
                )
                assert result.is_error
                assert not list(tmp_path.glob(".yoke-upload-*"))
            fresh = structured(
                await client.call_tool(
                    "write_binary_file", {"path": "fresh", "data_base64": "YQ=="}
                )
            )
            assert fresh["ok"], fresh
            assert (tmp_path / "fresh").read_bytes() == b"a"
            assert (tmp_path / "existing").read_text() == "preserved"

    asyncio.run(scenario())


def test_unicode_batch_preserves_wire_schema(tmp_path: Path) -> None:
    (tmp_path / "large").write_text("payload " * 10000)
    ids = ["😀" * 78 + str(i) for i in range(16)]

    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with memory_client(service) as client:
            schema = next(
                tool.output_schema
                for tool in (await client.list_tools()).tools
                if tool.name == "batch_read"
            )
            items = [
                {
                    "id": item_id,
                    "tool": "read_file",
                    "arguments": {"path": "large" if i % 2 else "missing"},
                }
                for i, item_id in enumerate(ids)
            ]
            rejected = await client.call_tool(
                "batch_read", {"items": items, "max_output_tokens": 3000}
            )
            assert rejected.is_error
            response = await client.call_tool(
                "batch_read", {"items": items, "max_output_tokens": 6400}
            )
            result = structured(response)
            assert not response.is_error, result
            assert schema is not None
            Draft202012Validator(schema).validate(result)
            assert [item["id"] for item in result["items"]] == ids
            assert [item["status"] for item in result["items"]] == [
                "ok" if i % 2 else "error" for i in range(16)
            ]
            assert len(json.dumps(result, ensure_ascii=True)) <= 25600
            assert any(
                item.get("data", {}).get("result_ref") for item in result["items"]
            )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "root_id", [None, "relative/schema.json", "https://example.org/schema.json"]
)
@pytest.mark.parametrize(
    "definition,reference,value",
    [
        ({"Item": {"type": "integer"}}, "#/$defs/Item", 3),
        ({"Item": {"$anchor": "item", "type": "integer"}}, "#item", 3),
        (
            {
                "Item": {
                    "$id": "child.json",
                    "$defs": {"Value": {"type": "integer"}},
                    "$ref": "#/$defs/Value",
                }
            },
            "child.json",
            3,
        ),
    ],
)
def test_wrapper_local_references(
    root_id: str | None, definition: dict[str, Any], reference: str, value: Any
) -> None:
    schema: dict[str, Any] = {"$defs": definition, "$ref": reference}
    if root_id:
        schema["$id"] = root_id
    original = deepcopy(schema)
    wrapper = Wrapper(
        name="downstream_fixture",
        server="fixture",
        tool="fixture",
        description="Fixture",
        input_schema={"type": "object"},
        output_schema=schema,
    )
    output_schema = wrapper.descriptor().output_schema
    assert output_schema is not None
    validator = Draft202012Validator(output_schema)
    validator.validate({"ok": True, "structuredContent": value})
    assert not validator.is_valid({"ok": True, "structuredContent": "wrong"})
    assert schema == original


def test_wrapper_recursive_root_reference() -> None:
    wrapper = Wrapper(
        name="downstream_fixture",
        server="fixture",
        tool="fixture",
        description="Fixture",
        input_schema={},
        output_schema={
            "type": "object",
            "properties": {"child": {"$ref": "#"}},
            "additionalProperties": False,
        },
    )
    output_schema = wrapper.descriptor().output_schema
    assert output_schema is not None
    validator = Draft202012Validator(output_schema)
    validator.validate({"ok": True, "structuredContent": {"child": {"child": {}}}})
    assert not validator.is_valid({"ok": True, "structuredContent": {"child": 1}})


def test_full_retention_store_preserves_batch_siblings() -> None:
    from yoke.mcp_server.results.batch import project_batch
    from yoke.mcp_server.results.store import ResultStore

    result = project_batch(
        {
            "ok": True,
            "run_id": "run",
            "operations": 2,
            "elapsed_ms": 1,
            "items": [
                {"id": "large", "status": "ok", "data": {"content": "x" * 10000}},
                {"id": "small", "status": "ok", "data": {"content": "kept"}},
            ],
        },
        ResultStore(max_bytes=1),
        2048,
    )
    assert result["items"][0]["status"] == "error"
    assert result["items"][1]["data"]["content"] == "kept"
    assert result["operations"] == 2
