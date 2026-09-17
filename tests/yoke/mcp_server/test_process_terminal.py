"""PTY readers remain alive across nonblocking stdin delivery."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys

import pytest

from yoke.mcp_server.config import MCPServerConfig
from yoke.mcp_server.server import create_service

from .helpers import memory_client, structured


def continuation(item: dict) -> dict:
    return {"session_id": item["session_id"], "cursor": item["cursor"]}


@pytest.mark.skipif(os.name == "nt", reason="POSIX PTY")
def test_mcp_pty_interaction_preserves_output_and_final_cursor(tmp_path: Path) -> None:
    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with memory_client(service) as client:
            running = structured(
                await client.call_tool(
                    "command_exec",
                    {
                        "argv": [
                            sys.executable,
                            "-u",
                            "-c",
                            "print('ready', flush=True); value=input(); print('got:'+value)",
                        ],
                        "tty": True,
                        "mode": "background",
                    },
                )
            )
            prompt = structured(
                await client.call_tool(
                    "process_read",
                    {
                        "sessions": [continuation(running)],
                        "until": "output_or_completion",
                        "wait_ms": 2000,
                    },
                )
            )["items"][0]
            reply = structured(
                await client.call_tool(
                    "process_input",
                    {
                        "session_id": running["session_id"],
                        "chars": "hello\n",
                        "cursor": prompt["cursor"],
                        "wait_ms": 2000,
                    },
                )
            )
            assert reply["ok"] and reply["input_written"]
            assert not reply["running"] and "got:hello\r\n" in reply["output"]
            assert not reply["has_more_output"] and reply["cursor"]
            final = structured(
                await client.call_tool(
                    "process_read",
                    {
                        "sessions": [continuation(reply)],
                        "wait_ms": 0,
                    },
                )
            )
            assert final["items"][0]["output"] == ""

    asyncio.run(scenario())
