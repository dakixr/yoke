"""Settlement recovers dropped live tool updates from owned native calls."""

from __future__ import annotations

import json
import os
import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
from acp import schema as s

from tests.yoke.acp.test_bridge import FixtureNative
from yoke.acp.bridge import YokeAcpAgent


class DroppedToolsNative(FixtureNative):
    def __init__(self) -> None:
        super().__init__()
        self.delivery = "none"
        self.trace_available = True
        self.continuation = False
        self.result = {
            "ok": True,
            "session_id": 7,
            "running": True,
            "status": "running",
            "exit_code": None,
            "output": "launched",
        }

    async def respond(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/tool-call/recovered"):
            if not self.trace_available:
                return httpx.Response(404)
            return httpx.Response(
                200,
                json={
                    "data": {
                        "id": "recovered",
                        "toolName": "command_exec",
                        "status": "ok",
                        "arguments": {
                            "raw": '{"cmd":"sleep 5"}',
                            "executed": {"cmd": "sleep 5"},
                        },
                        "result": self.result,
                    }
                },
            )
        if path.endswith("/message"):
            assert self.admission is not None
            input_id = self.admission["id"]
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"type": "user", "inputID": "previous", "content": []},
                        {
                            "type": "assistant",
                            "inputID": "previous",
                            "content": [],
                            "toolCalls": [
                                {"id": "unrelated", "name": "custom", "arguments": "{}"}
                            ],
                        },
                        *(
                            []
                            if self.continuation
                            else [{"type": "user", "inputID": input_id, "content": []}]
                        ),
                        {
                            "type": "assistant",
                            "inputID": input_id,
                            "content": [],
                            "toolCalls": [
                                {
                                    "id": "recovered",
                                    "name": "command_exec",
                                    "arguments": '{"cmd":"sleep 5"}',
                                },
                            ],
                        },
                        {
                            "type": "tool",
                            "callID": "recovered",
                            "result": json.dumps(self.result),
                        },
                        {
                            "type": "assistant",
                            "inputID": input_id,
                            "content": [{"type": "text", "text": "Done."}],
                        },
                        {
                            "type": "assistant",
                            "inputID": "later",
                            "content": [],
                            "toolCalls": [
                                {
                                    "id": "later-call",
                                    "name": "custom",
                                    "arguments": "{}",
                                },
                            ],
                        },
                    ],
                    "cursor": {"next": None},
                },
            )
        response = await super().respond(request)
        if path.endswith("/prompt"):
            assert self.queue is not None and self.admission is not None
            while not self.queue.empty():
                self.queue.get_nowait()
            if self.delivery in ("start", "both"):
                self.queue.put_nowait(
                    {
                        "type": "session.tool.started",
                        "sessionID": "native-id",
                        "data": {
                            "inputID": self.admission["id"],
                            "tool_call_id": "recovered",
                            "tool_name": "command_exec",
                            "tool_arguments": '{"cmd":"sleep 5"}',
                        },
                    }
                )
            if self.delivery == "both":
                self.queue.put_nowait(
                    {
                        "type": "session.tool.ended",
                        "sessionID": "native-id",
                        "data": {
                            "inputID": self.admission["id"],
                            "tool_call_id": "recovered",
                            "tool_name": "command_exec",
                            "executed_arguments": {"cmd": "sleep 5"},
                            "ok": True,
                            "result": self.result,
                        },
                    }
                )
        return response


class ReconciliationTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_or_partial_updates_recover_exactly_once(self) -> None:
        for delivery in ("none", "start", "both"):
            for continuation in (False, True):
                native = DroppedToolsNative()
                native.delivery, native.continuation = delivery, continuation
                agent = YokeAcpAgent(native)
                updates: list[dict[str, Any]] = []
                conn = AsyncMock()

                async def collect(session_id, update):
                    s.SessionNotification.model_validate(
                        {"sessionId": session_id, "update": update}
                    )
                    updates.append(update)

                conn.session_update.side_effect = collect
                agent.on_connect(conn)
                try:
                    await agent.new_session(os.getcwd())
                    with patch.object(
                        native.process_monitor, "track", new=AsyncMock()
                    ) as monitor:
                        await agent.prompt(
                            "native-id",
                            []
                            if continuation
                            else [s.TextContentBlock(type="text", text="do it")],
                            yokeContinuation=continuation,
                        )
                        monitor.assert_awaited_once_with(
                            "native-id", 7, "recovered", agent.update
                        )
                    assert [item["sessionUpdate"] for item in updates] == [
                        "tool_call",
                        "tool_call_update",
                        "agent_message_chunk",
                    ]
                    assert updates[1]["rawOutput"] == native.result
                    assert (
                        updates[0]["toolCallId"]
                        == updates[1]["toolCallId"]
                        == "recovered"
                    )
                finally:
                    await agent.close()

    async def test_missing_inspector_result_remains_unconfirmed(self) -> None:
        native = DroppedToolsNative()
        native.trace_available = False
        agent = YokeAcpAgent(native)
        conn = AsyncMock()
        agent.on_connect(conn)
        try:
            await agent.new_session(os.getcwd())
            result = await agent.prompt(
                "native-id", [s.TextContentBlock(type="text", text="do it")]
            )
            assert result.stop_reason == "end_turn"
            end = conn.session_update.call_args_list[-1].kwargs["update"]
            assert end["status"] == "failed"
            assert "unavailable" in end["content"][0]["content"]["text"]
            assert "rawOutput" not in end
        finally:
            await agent.close()
