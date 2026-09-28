"""Exercise presentation through the ACP turn and native transport contracts."""

from __future__ import annotations

import asyncio
import copy
import json
import os
import unittest
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
from acp import schema as s

from tests.yoke.acp.test_bridge import FixtureNative
from yoke.acp.bridge import YokeAcpAgent
from yoke.acp.native import NativeClient
from yoke.acp.tools import ToolCallProjector
from yoke.http.models.process import ProcessInfo, ProcessOutputInfo


class ToolNative(FixtureNative):
    def __init__(self) -> None:
        super().__init__()
        self.tool = "read"
        self.arguments = '{"path":"src/app.py","offset":9}'
        self.result: dict[str, Any] | None = None
        self.reason = "end_turn"
        self.error = False

    async def turn(self, session_id, text, input_id, work, emit, **kwargs) -> str:
        await emit(
            "session.tool.started",
            {
                "tool_call_id": "owned-call",
                "tool_name": self.tool,
                "tool_arguments": self.arguments,
            },
        )
        if self.result is not None:
            await emit(
                "session.tool.ended",
                {
                    "tool_call_id": "owned-call",
                    "tool_name": self.tool,
                    "ok": self.result.get("ok", True),
                    "result": self.result,
                },
            )
        if self.error:
            raise RuntimeError("original native failure")
        return self.reason


class ToolActivityTurnTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.native = ToolNative()
        self.agent = YokeAcpAgent(self.native)
        self.updates: list[dict[str, Any]] = []
        self.agent.on_connect(self)
        await self.agent.new_session(os.getcwd())

    async def asyncTearDown(self) -> None:
        await self.agent.close()

    async def session_update(self, session_id: str, update: dict[str, Any]) -> None:
        s.SessionNotification.model_validate(
            {"sessionId": session_id, "update": update}
        )
        self.updates.append(update)

    async def prompt(self):
        return await self.agent.prompt(
            "native-id", [s.TextContentBlock(type="text", text="unchanged prompt")]
        )

    async def test_relative_location_uses_native_session_directory(self) -> None:
        self.native.result = {"ok": True, "content": "body"}
        await self.prompt()
        start, end = self.updates
        assert (
            start["locations"]
            == end["locations"]
            == [{"path": os.path.join(os.getcwd(), "src/app.py"), "line": 9}]
        )
        assert end["rawInput"]["path"] == "src/app.py"

    async def test_cancelled_turn_closes_unconfirmed_tool(self) -> None:
        self.native.reason = "cancelled"
        result = await self.prompt()
        assert result.stop_reason == "cancelled"
        assert self.updates[-1]["status"] == "failed"
        assert self.updates[-1]["toolCallId"] == "owned-call"
        assert "interrupted" in self.updates[-1]["content"][0]["content"]["text"]
        assert "rawOutput" not in self.updates[-1]
        assert not self.agent.active

    async def test_missing_tool_result_is_never_reported_as_success(self) -> None:
        await self.prompt()
        assert self.updates[-1]["status"] == "failed"
        assert "unavailable" in self.updates[-1]["content"][0]["content"]["text"]

    async def test_original_error_survives_cleanup(self) -> None:
        self.native.error = True
        with self.assertRaisesRegex(RuntimeError, "original native failure"):
            await self.prompt()
        assert self.updates[-1]["status"] == "failed"
        assert not self.agent.active

    async def test_running_launch_registers_monitor_after_tool_completion(self) -> None:
        self.native.tool = "command_exec"
        self.native.arguments = '{"cmd":"sleep 5"}'
        self.native.result = {
            "ok": True,
            "session_id": 17,
            "status": "running",
            "running": True,
            "output": "starting",
            "exit_code": None,
        }
        original = copy.deepcopy(self.native.result)

        async def track(session_id, runtime_id, tool_call_id, update):
            assert (session_id, runtime_id, tool_call_id) == (
                "native-id",
                17,
                "owned-call",
            )
            assert self.updates[-1]["status"] == "completed"
            assert self.updates[-1]["rawOutput"] == original
            assert update == self.agent.update

        with patch.object(
            self.native.process_monitor, "track", new=AsyncMock(side_effect=track)
        ) as monitor:
            await self.prompt()
            monitor.assert_awaited_once()
        assert len(self.updates) == 2
        assert self.native.result == original

    async def test_foreground_exit_and_non_process_tool_do_not_start_monitor(
        self,
    ) -> None:
        for tool, result in [
            (
                "command_exec",
                {"ok": True, "running": False, "session_id": 3, "exit_code": 0},
            ),
            ("custom_tool", {"ok": True, "running": True, "session_id": 3}),
            ("python_exec", {"ok": True, "running": True, "session_id": True}),
        ]:
            self.native.tool, self.native.result = tool, result
            with patch.object(
                self.native.process_monitor, "track", new=AsyncMock()
            ) as monitor:
                await self.prompt()
                monitor.assert_not_awaited()

    async def test_process_read_reestablishes_observation_of_live_jobs_only(
        self,
    ) -> None:
        self.native.tool = "process_read"
        self.native.arguments = '{"sessions":[{"session_id":11},{"session_id":12}]}'
        self.native.result = {
            "ok": True,
            "items": [
                {"session_id": 11, "running": True},
                {"session_id": 12, "running": False, "exit_code": 0},
            ],
        }
        with patch.object(
            self.native.process_monitor, "track", new=AsyncMock()
        ) as monitor:
            await self.prompt()
            monitor.assert_awaited_once_with(
                "native-id", 11, "owned-call", self.agent.update
            )


class ToolPresentationEdgeTests(unittest.TestCase):
    def test_snapshot_does_not_claim_monitoring(self) -> None:
        tools = ToolCallProjector()
        update = tools.update(
            start=True,
            data={
                "tool_call_id": "snapshot",
                "tool_name": "process_read",
                "tool_arguments": '{"sessions":[{"session_id":1}],"wait_ms":0}',
            },
        )
        assert update["title"] == "Read process output · 1"

    def test_raw_patch_targets_match_native_parser(self) -> None:
        raw = "*** Begin Patch\n*** Add File: src/new.py\n+hello\n*** End Patch"
        for arguments in (raw, json.dumps(raw)):
            tools = ToolCallProjector("/native/repo")
            update = tools.update(
                start=True,
                data={
                    "tool_call_id": "patch",
                    "tool_name": "apply_patch",
                    "tool_arguments": arguments,
                },
            )
            assert update["kind"] == "edit"
            assert update["locations"] == [{"path": "/native/repo/src/new.py"}]
            assert update["rawInput"]["input"] == raw

    def test_cleanup_only_closes_remaining_calls_once(self) -> None:
        tools = ToolCallProjector()
        for call in ("ended", "missing"):
            tools.update(start=True, data={"tool_call_id": call, "tool_name": "custom"})
        tools.update(start=False, data={"tool_call_id": "ended", "ok": True})
        updates = tools.finish(interrupted=False)
        assert [item["toolCallId"] for item in updates] == ["missing"]
        assert tools.finish(interrupted=False) == []


class WireNative(NativeClient):
    async def events(self, queue, ready) -> None:
        ready.set()
        await asyncio.Event().wait()


class NativeProcessWireTests(unittest.IsolatedAsyncioTestCase):
    async def test_monitor_consumes_real_process_schema_aliases(self) -> None:
        info = ProcessInfo(
            process_id="proc_real_wire",
            session_id="native-owner",
            runtime_session_id=7,
            pid=123,
            command="echo hi",
            cwd="/repo",
            tty=False,
            status="running",
            started_at="2026-09-28T12:00:00Z",
            elapsed_ms=10,
            output=ProcessOutputInfo(
                tail="hello",
                original_bytes=5,
                retained_bytes=5,
                truncated=False,
                latest_seq=1,
            ),
        ).model_dump(by_alias=True)
        messages: list[dict[str, Any]] = []

        async def update(session_id, payload):
            assert session_id == "native-owner"
            messages.append(payload)

        def respond(request):
            assert request.method == "GET"
            if request.url.path.endswith("/process"):
                assert request.url.params["sessionID"] == "native-owner"
                return httpx.Response(200, json={"data": [info]})
            assert request.url.path.endswith("/process/proc_real_wire")
            return httpx.Response(200, json={"data": info})

        native = WireNative("http://fixture", "unused")
        await native.http.aclose()
        native.http = httpx.AsyncClient(
            base_url="http://fixture/api/v1/",
            transport=httpx.MockTransport(respond),
        )
        try:
            await native.process_monitor.track("native-owner", 7, "launch", update)
            assert len(messages) == 1
            assert messages[0]["rawOutput"]["status"] == "running"
            assert messages[0]["toolCallId"] == "yoke-process:proc_real_wire"
        finally:
            await native.close()
