"""Process pages cannot lose their continuation metadata to generic previews."""

import asyncio
import json
from pathlib import Path
from typing import Any

from mcp.types import TextContent
import pytest

from yoke.agent.tools.command import CommandExecTool
from yoke.agent.tools.processes.cursor import encode_cursor, ProcessPosition
from yoke.mcp_server.config import MCPServerConfig
from yoke.mcp_server.results.encoding import encode
from yoke.mcp_server.results.store import ResultStore
from yoke.mcp_server.server import create_service

from .helpers import memory_client


@pytest.mark.parametrize("envelope", [False, True])
@pytest.mark.parametrize("legacy_text", [False, True])
def test_process_page_stays_exact_when_encoded_page_exceeds_text_budget(
    envelope: bool, legacy_text: bool
) -> None:
    item = {
        "ok": True,
        "session_id": 42,
        "running": False,
        "exit_code": 7,
        "timed_out": False,
        "output": '\\"界\n' * 1000,
        "elapsed_seconds": 1.5,
        "cursor": encode_cursor(ProcessPosition(42, after_seq=3, offset=100)),
        "has_more_output": True,
        "gap": True,
        "next_tool": "process_read",
        "reason": "completed",
    }
    value = {"ok": True, "reason": "completed", "items": [item]} if envelope else item
    # Retention cannot rescue a hidden process cursor when the store is full.
    result = encode(
        value,
        ResultStore(max_bytes=1),
        budget=512,
        legacy_text=legacy_text,
        process=True,
    )
    assert result.structured_content == value
    assert isinstance(result.content[0], TextContent)
    assert json.loads(result.content[0].text) == value
    assert not result.is_error


@pytest.mark.parametrize("full_store", [False, True])
def test_recipe_keeps_runner_cursor_outside_retained_report(full_store: bool) -> None:
    execution = {
        "ok": True,
        "session_id": 7,
        "running": True,
        "exit_code": None,
        "output": "ready\n",
        "cursor": encode_cursor(ProcessPosition(7, after_seq=1)),
        "has_more_output": False,
        "gap": False,
    }
    encoded = encode(
        {"ok": True, "patch": {"output": "x" * 10000}, "execution": execution},
        ResultStore(max_bytes=1) if full_store else ResultStore(),
        budget=1024,
        process_recipe=True,
    )
    assert encoded.structured_content is not None
    assert encoded.structured_content["execution"] == execution
    assert encoded.structured_content["report_omitted" if full_store else "result_ref"]
    assert isinstance(encoded.content[0], TextContent)
    assert json.loads(encoded.content[0].text)["execution"] == execution


@pytest.mark.parametrize("shape", ["item", "batch", "recipe"])
def test_downstream_cursor_shaped_data_cannot_bypass_output_limits(shape: str) -> None:
    item = {
        "ok": True,
        "session_id": 7,
        "cursor": encode_cursor(ProcessPosition(7)),
        "output": "x" * 100_000,
    }
    value = (
        {"ok": True, "items": [item]}
        if shape == "batch"
        else ({"ok": True, "execution": item} if shape == "recipe" else item)
    )
    store = ResultStore()
    try:
        encoded = encode(value, store, budget=1024)
        assert encoded.structured_content is not None
        assert encoded.structured_content["truncated"] is True
        assert "result_ref" in encoded.structured_content
        assert len(json.dumps(encoded.structured_content)) < 3000
    finally:
        store.close()


@pytest.mark.parametrize("full_store", [False, True])
@pytest.mark.parametrize("shape", ["error", "cursor_error", "batch_error"])
def test_non_page_process_errors_remain_bounded_json(
    full_store: bool, shape: str
) -> None:
    error: dict[str, Any] = {
        "ok": False,
        "reason": "error",
        "error": "invalid\n" * 100_000,
    }
    if shape == "cursor_error":
        error.update(session_id=7, cursor=encode_cursor(ProcessPosition(7)))
    value = (
        {"ok": False, "reason": "error", "items": [error]}
        if shape == "batch_error"
        else error
    )
    store = ResultStore(max_bytes=1) if full_store else ResultStore()
    try:
        encoded = encode(value, store, budget=1024, process=True)
        assert encoded.is_error
        assert encoded.structured_content is not None
        assert encoded.structured_content["truncated"] is True
        assert encoded.structured_content["reason"] == "error"
        assert len(json.dumps(encoded.structured_content)) <= 1024
        assert isinstance(encoded.content[0], TextContent)
        assert json.loads(encoded.content[0].text) == encoded.structured_content
        if not full_store:
            retained = store.read(encoded.structured_content["result_ref"], limit=100)
            assert retained["content"]
    finally:
        store.close()


def test_adapter_bounds_non_page_command_error_with_configured_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        CommandExecTool,
        "execute",
        lambda self: {"ok": False, "reason": "error", "error": "invalid" * 100_000},
    )

    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path, max_output_tokens=256))
        async with memory_client(service):
            result = await service.adapter.call_tool("command_exec", {"cmd": "unused"})
            assert result.is_error
            assert result.structured_content is not None
            assert result.structured_content["truncated"]
            assert result.structured_content["reason"] == "error"
            assert len(json.dumps(result.structured_content)) <= 1024
            assert isinstance(result.content[0], TextContent)
            assert json.loads(result.content[0].text) == result.structured_content

    asyncio.run(scenario())


@pytest.mark.parametrize("full_store", [False, True])
@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("budget", [4, 1024])
def test_projection_preserves_partial_input_delivery_and_cancellation(
    full_store: bool, batch: bool, budget: int
) -> None:
    item = {
        "ok": False,
        "reason": "cancelled",
        "cancelled": True,
        "session_id": 12345,
        "input_written": None,
        "input_bytes_written": 4096,
        "error": "Input stopped " * 1000,
    }
    value = {"ok": False, "reason": "cancelled", "items": [item]} if batch else item
    store = ResultStore(max_bytes=1) if full_store else ResultStore()
    try:
        encoded = encode(value, store, budget=budget, process=True)
        payload = encoded.structured_content
        assert payload is not None and not payload["ok"]
        assert payload["reason"] == "cancelled"
        result = payload["items"][0] if batch else payload
        for key in (
            "reason",
            "session_id",
            "input_written",
            "input_bytes_written",
            "cancelled",
        ):
            assert result[key] == item[key]
        assert len(json.dumps(payload)) <= max(1024, budget)
        assert isinstance(encoded.content[0], TextContent)
        assert json.loads(encoded.content[0].text) == payload
    finally:
        store.close()
