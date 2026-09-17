"""Public process request policy and non-consuming observation contracts."""

from __future__ import annotations

import asyncio
from pathlib import Path
import time

import pytest

from yoke.agent.tools.processes.cursor import encode_cursor, ProcessPosition
from yoke.mcp_server.config import MCPServerConfig
from yoke.mcp_server.server import create_service

from .helpers import memory_client, structured


def continuation(item: dict) -> dict:
    value = {"session_id": item["session_id"]}
    if item.get("cursor") is not None:
        value["cursor"] = item["cursor"]
    return value


@pytest.mark.parametrize(
    "name,arguments",
    [
        ("command_exec", {"argv": ["unused"]}),
        ("python_exec", {"code": "raise AssertionError('must not run')"}),
        ("process_input", {"session_id": 1, "chars": "x"}),
        ("process_read", {"sessions": [{"session_id": 1}]}),
        ("process_cancel", {"session_id": 1}),
    ],
)
def test_legacy_wait_field_is_rejected(
    tmp_path: Path, name: str, arguments: dict
) -> None:
    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with memory_client(service):
            result = structured(
                await service.adapter.call_tool(name, {**arguments, "yield_time_ms": 0})
            )
            assert result["error_code"] == "INVALID_ARGUMENT"
            assert result["execution_started"] is False
            assert not service.runtime.manager.snapshots()

    asyncio.run(scenario())


def test_configured_defaults_background_and_fixed_input_budget(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = create_service(
            MCPServerConfig(
                root=tmp_path, default_yield_ms=1234, max_remote_wait_ms=5000
            )
        )
        async with memory_client(service):
            adapter = service.adapter
            for name in ("command_exec", "python_exec"):
                assert "wait_ms" not in adapter._with_runtime_defaults(name, {})
                assert "wait_ms" not in adapter._with_runtime_defaults(
                    name, {"mode": "background"}
                )
            tools = {tool.name: tool for tool in adapter.list_tools()}
            read = tools["process_read"].input_schema["properties"]
            assert read["wait_ms"]["default"] == read["wait_ms"]["maximum"] == 5000
            assert read["until"]["default"] == "completion"
            input_schema = tools["process_input"].input_schema
            assert "chars" in input_schema["required"]
            assert input_schema["properties"]["chars"]["minLength"] == 1
            assert input_schema["properties"]["wait_ms"]["default"] == 250
            assert input_schema["properties"]["wait_ms"]["maximum"] == 5000
            for name in ("command_exec", "python_exec"):
                fields = tools[name].input_schema["properties"]
                assert fields["mode"]["default"] == "auto"
                assert "wait_ms" not in fields
                assert "yield_time_ms" not in fields
                rejected = structured(
                    await adapter.call_tool(
                        name,
                        {
                            **(
                                {"argv": ["unused"]}
                                if name == "command_exec"
                                else {"code": "pass"}
                            ),
                            "wait_ms": 1,
                        },
                    )
                )
                assert rejected["error_code"] == "INVALID_ARGUMENT"
            assert not service.runtime.manager.snapshots()

    asyncio.run(scenario())


@pytest.mark.parametrize("with_cursor", [False, True])
def test_input_requires_chars_and_collects_response_without_skipping_history(
    tmp_path: Path, with_cursor: bool
) -> None:
    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with memory_client(service) as client:
            started = structured(
                await client.call_tool(
                    "python_exec",
                    {
                        "code": "print('ready', flush=True); print('echo:' + input(), flush=True)",
                        "mode": "background",
                    },
                )
            )
            session = started["session_id"]
            ready = structured(
                await client.call_tool(
                    "process_read",
                    {
                        "sessions": [{"session_id": session}],
                        "until": "output_or_completion",
                        "wait_ms": 5000,
                    },
                )
            )["items"][0]
            assert ready["output"] == "ready\n"
            for extra in ({}, {"chars": ""}, {"chars": "bad\n", "wait_ms": 5001}):
                rejected = structured(
                    await client.call_tool(
                        "process_input", {"session_id": session, **extra}
                    )
                )
                assert rejected["error_code"] == "INVALID_ARGUMENT"
            mismatch = structured(
                await client.call_tool(
                    "process_input",
                    {
                        "session_id": session,
                        "chars": "bad\n",
                        "cursor": encode_cursor(
                            ProcessPosition(session_id=session + 1)
                        ),
                    },
                )
            )
            assert not mismatch["ok"]
            result = structured(
                await client.call_tool(
                    "process_input",
                    {
                        "session_id": session,
                        "chars": "value\n",
                        "wait_ms": 5000,
                        **({"cursor": ready["cursor"]} if with_cursor else {}),
                    },
                )
            )
            assert result["ok"]
            assert result["running"] is False
            assert result["exit_code"] == 0
            assert (
                result["output"] == ("" if with_cursor else "ready\n") + "echo:value\n"
            )
            assert isinstance(result["cursor"], str) and result["cursor"].startswith(
                "pc1_"
            )

    asyncio.run(scenario())


def test_completion_waits_for_all_but_output_wait_accepts_any(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with memory_client(service) as client:
            finished = structured(
                await client.call_tool(
                    "python_exec", {"code": "print('done'); raise SystemExit(7)"}
                )
            )
            assert finished["ok"] is False
            running = structured(
                await client.call_tool(
                    "python_exec",
                    {"code": "input()", "mode": "background"},
                )
            )
            sessions = [continuation(finished), continuation(running)]
            observed = structured(
                await client.call_tool(
                    "process_read",
                    {
                        "sessions": sessions,
                        "until": "output_or_completion",
                        "wait_ms": 5000,
                    },
                )
            )
            assert observed["ok"]
            assert observed["reason"] == "completed"
            assert observed["items"][0]["exit_code"] == 7
            assert observed["items"][0]["ok"]
            assert observed["items"][1]["running"]
            before = time.monotonic()
            pending = structured(
                await client.call_tool(
                    "process_read", {"sessions": sessions, "wait_ms": 80}
                )
            )
            assert time.monotonic() - before >= 0.06
            assert pending["reason"] == "deadline"
            assert pending["items"][1]["running"]
            await client.call_tool(
                "process_cancel", {"session_id": running["session_id"]}
            )
            done = structured(
                await client.call_tool(
                    "process_read", {"sessions": sessions, "wait_ms": 5000}
                )
            )
            assert done["reason"] == "completed"
            assert done["ok"]
            assert all(not item["running"] for item in done["items"])
            snapshot = structured(
                await client.call_tool(
                    "process_read", {"sessions": sessions, "wait_ms": 0}
                )
            )
            assert snapshot["reason"] == "snapshot"

    asyncio.run(scenario())


def test_finished_cancel_keeps_pageable_output(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path, max_output_tokens=100))
        async with memory_client(service) as client:
            started = structured(
                await client.call_tool("python_exec", {"code": "print('界' * 1000)"})
            )
            assert not started["running"] and started["has_more_output"]
            for _ in range(2):
                cancelled = structured(
                    await client.call_tool(
                        "process_cancel", {"session_id": started["session_id"]}
                    )
                )
                assert cancelled["ok"]
            remaining = structured(
                await client.call_tool(
                    "process_read", {"sessions": [continuation(started)], "wait_ms": 0}
                )
            )["items"][0]
            assert started["output"] + remaining["output"] == "界" * 1000 + "\n"

    asyncio.run(scenario())


def test_invalid_cursors_return_prompt_ordered_errors(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with memory_client(service) as client:
            finished = structured(
                await client.call_tool("python_exec", {"code": "print('界')"})
            )
            session = finished["session_id"]
            for cursor in (
                encode_cursor(ProcessPosition(session, after_seq=999_999)),
                encode_cursor(ProcessPosition(session, offset=1)),
                encode_cursor(ProcessPosition(session, after_seq=1, offset=1)),
            ):
                before = time.monotonic()
                result = structured(
                    await client.call_tool(
                        "process_read",
                        {
                            "sessions": [{"session_id": session, "cursor": cursor}],
                            "wait_ms": 240_000,
                        },
                    )
                )
                assert time.monotonic() - before < 2
                assert result["reason"] == "error"
                assert not result["ok"]
                assert not result["items"][0]["ok"]
                error = result["items"][0]["error"]
                assert error == "Invalid process cursor"
                assert "offset" not in error and "sequence" not in error
            missing = session + 1
            result = structured(
                await client.call_tool(
                    "process_read",
                    {
                        "sessions": [{"session_id": missing}, continuation(finished)],
                        "wait_ms": 240_000,
                    },
                )
            )
            assert result["reason"] == "error"
            assert [item["session_id"] for item in result["items"]] == [
                missing,
                session,
            ]
            assert not result["items"][0]["ok"] and result["items"][1]["ok"]
            duplicate = structured(
                await client.call_tool(
                    "process_read", {"sessions": [continuation(finished)] * 2}
                )
            )
            assert duplicate["error_code"] == "INVALID_ARGUMENT"

    asyncio.run(scenario())


@pytest.mark.parametrize("chatty", [False, True])
def test_completion_deadline_ignores_intermediate_output(
    tmp_path: Path, chatty: bool
) -> None:
    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with memory_client(service) as client:
            code = (
                "import time\nwhile True:\n print('x' * 2000, flush=True)\n time.sleep(.01)"
                if chatty
                else "input()"
            )
            started = structured(
                await client.call_tool(
                    "python_exec", {"code": code, "mode": "background"}
                )
            )
            before = time.monotonic()
            result = structured(
                await client.call_tool(
                    "process_read",
                    {
                        "sessions": [continuation(started)],
                        "wait_ms": 200,
                        "max_bytes": 1024,
                    },
                )
            )
            assert time.monotonic() - before >= 0.15
            assert result["reason"] == "deadline"
            item = result["items"][0]
            assert item["running"] and not item["timed_out"]
            assert len(item["output"].encode()) <= 1024
            await client.call_tool(
                "process_cancel", {"session_id": started["session_id"]}
            )

    asyncio.run(scenario())
