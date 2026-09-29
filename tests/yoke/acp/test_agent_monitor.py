"""Read-only agent notifications without a daemon, process tools, or model calls."""

from __future__ import annotations

import asyncio
import os
from typing import Any
import unittest

from acp import schema as s
import httpx

from yoke.acp.agent_monitor import AgentMonitor
from yoke.acp.bridge import YokeAcpAgent
from yoke.acp.native import NativeClient
from yoke.acp.observation.agents import AgentRoster
from yoke.acp.tools import ToolCallProjector


def snapshot(**changes: Any) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "agentId": "agent",
        "runId": "run",
        "sessionID": "owner",
        "runtimeSessionID": 7,
        "name": "Reviewer",
        "provider": "fixture",
        "model": "model",
        "status": "running",
        "observation": "live",
        "startedAt": "2026-09-28T12:00:00+00:00",
        "finishedAt": None,
        "lastSeenAt": "2026-09-28T12:00:00+00:00",
        "updatedAt": "2026-09-28T12:00:00+00:00",
        "version": 1,
        **changes,
    }


class Native(NativeClient):
    def __init__(self) -> None:
        super().__init__("http://fixture", "secret")
        self.http = httpx.AsyncClient(
            base_url="http://fixture/api/v1/",
            transport=httpx.MockTransport(self.respond),
        )
        self.items = [snapshot()]
        self.calls: list[tuple[str, str]] = []
        self.supported = True
        self.discovery_status = 200
        self.status = 200
        self.session_status: dict[str, int] = {}
        self.read_count = 0
        self.reads: asyncio.Queue[None] = asyncio.Queue()
        self.streams: asyncio.Queue[asyncio.Event] = asyncio.Queue()
        self.queue: asyncio.Queue[dict[str, Any]] | None = None
        self.stream_count = 0

    async def respond(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1/")
        self.calls.append((request.method, path))
        if path == "openapi.json":
            return httpx.Response(
                self.discovery_status,
                json={
                    "paths": {"/api/v1/agent-run": {"get": {}}}
                    if self.supported
                    else {}
                },
            )
        if path == "agent-run":
            self.read_count += 1
            self.reads.put_nowait(None)
            assert request.method == "GET"
            assert request.url.params["sessionID"] in {"owner", "other"}
            return httpx.Response(
                self.session_status.get(request.url.params["sessionID"], self.status),
                json={"data": self.items},
            )
        if path == "provider":
            data: Any = [{"id": "fixture", "ready": True, "currentModel": "model"}]
        elif path == "model":
            data = [
                {
                    "id": "model",
                    "provider": "fixture",
                    "name": "Fixture",
                    "reasoningEfforts": [],
                    "capabilities": {"images": False},
                }
            ]
        elif path in {"session", "session/owner"}:
            data = {
                "id": "owner",
                "location": {"directory": os.getcwd()},
                "selection": {"provider": "fixture", "model": "model"},
            }
        elif path == "session/owner/message":
            return httpx.Response(200, json={"data": [], "cursor": {"next": None}})
        else:
            raise AssertionError(
                f"Unexpected observation request {request.method} {path}"
            )
        return httpx.Response(200, json={"data": data})

    async def events(self, queue, ready) -> None:
        self.queue = queue
        disconnect = asyncio.Event()
        self.stream_count += 1
        self.streams.put_nowait(disconnect)
        ready.set()
        try:
            await disconnect.wait()
            raise RuntimeError("native disconnected")
        finally:
            self.stream_count -= 1

    def invalidate(
        self, session_id: str = "owner", kind: str = "session.agent.updated"
    ) -> None:
        assert self.queue is not None
        self.queue.put_nowait({"type": kind, "sessionID": session_id, "data": {}})


class AgentMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.native = Native()
        self.ticks: asyncio.Queue[None] = asyncio.Queue()
        self.retries: asyncio.Queue[None] = asyncio.Queue()
        self.native.agent_monitor = AgentMonitor(
            self.native,
            reconciliation_tick=self.ticks.get,
            reconnect_tick=self.retries.get,
        )
        self.monitor = self.native.agent_monitor
        self.messages: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

    async def asyncTearDown(self) -> None:
        await self.native.close()
        self.assertEqual(self.native.stream_count, 0)
        self.assertFalse(self.monitor._sessions)
        self.assertIsNone(self.monitor._watch_task)

    async def update(self, session_id: str, update: dict[str, Any]) -> None:
        s.SessionNotification.model_validate(
            {"sessionId": session_id, "update": update}
        )
        self.messages.put_nowait(update)

    async def next(self) -> dict[str, Any]:
        return await asyncio.wait_for(self.messages.get(), 2)

    async def read_until(self, count: int) -> None:
        while self.native.read_count < count:
            await asyncio.wait_for(self.native.reads.get(), 2)
        async with self.monitor._lock:
            pass

    async def attach(self) -> asyncio.Event:
        await self.monitor.attach("owner", self.update)
        disconnect = await asyncio.wait_for(self.native.streams.get(), 2)
        await self.read_until(2)
        return disconnect

    async def test_initial_roster_then_new_agents_and_terminal_updates_after_turn(
        self,
    ) -> None:
        await self.attach()
        start = await self.next()
        self.assertEqual(start["sessionUpdate"], "tool_call")
        self.assertEqual(start["toolCallId"], "yoke-agent:agent")
        self.assertEqual(start["rawOutput"], {"type": "yoke_agent", **snapshot()})
        self.assertEqual(start["status"], "in_progress")
        # There is no prompt task, active-turn state, or process_read call.
        self.native.items.append(
            snapshot(agentId="second", runId="second-run", version=2)
        )
        self.native.invalidate()
        second = await self.next()
        self.assertEqual(second["toolCallId"], "yoke-agent:second")
        self.native.items[0] = snapshot(status="completed", version=3)
        self.native.invalidate()
        terminal = await self.next()
        self.assertEqual(terminal["sessionUpdate"], "tool_call_update")
        self.assertEqual(terminal["status"], "completed")
        self.assertEqual(terminal["rawOutput"]["version"], 3)
        self.assertTrue(all(method == "GET" for method, _ in self.native.calls))

    async def test_initial_empty_roster_still_watches_and_missed_event_reconciles(
        self,
    ) -> None:
        self.native.items = []
        await self.attach()
        self.assertTrue(self.messages.empty())
        self.native.items = [snapshot()]
        self.ticks.put_nowait(None)
        self.assertEqual((await self.next())["status"], "in_progress")
        self.native.items = [snapshot(status="cancelled", version=2)]
        self.ticks.put_nowait(None)
        terminal = await self.next()
        self.assertEqual(terminal["status"], "failed")
        self.assertEqual(terminal["rawOutput"]["status"], "cancelled")

    async def test_disconnect_is_lost_not_success_and_reconnect_restores_same_revision(
        self,
    ) -> None:
        disconnect = await self.attach()
        await self.next()
        disconnect.set()
        lost = await self.next()
        self.assertEqual(lost["rawOutput"]["observation"], "lost")
        self.assertEqual(lost["rawOutput"]["status"], "running")
        self.assertEqual(lost["rawOutput"]["version"], 1)
        self.assertEqual(lost["status"], "failed")
        self.assertIn("unconfirmed", lost["content"][0]["content"]["text"])
        self.retries.put_nowait(None)
        await asyncio.wait_for(self.native.streams.get(), 2)
        recovered = await self.next()
        self.assertEqual(recovered["rawOutput"]["observation"], "live")
        self.assertEqual(recovered["rawOutput"]["version"], 1)
        self.assertEqual(recovered["status"], "in_progress")

    async def test_isolation_duplicates_and_late_previous_run_completion(self) -> None:
        self.native.items.extend(
            [
                snapshot(agentId="foreign", sessionID="other"),
                {"toolCallId": "yoke-agent:spoof"},
            ]
        )
        await self.attach()
        await self.next()
        self.native.items = [
            snapshot(runId="new-run", startedAt="2026-09-28T13:00:00+00:00", version=4)
        ]
        self.native.invalidate()
        new = await self.next()
        self.assertEqual(new["toolCallId"], "yoke-agent:agent")
        self.assertEqual(new["rawOutput"]["runId"], "new-run")
        self.native.items.append(snapshot(status="completed", version=5))
        reads = self.native.read_count
        self.native.invalidate("other")
        self.native.invalidate(kind="session.tool.ended")
        self.native.invalidate()
        await self.read_until(reads + 1)
        self.assertTrue(self.messages.empty())
        self.native.items = [
            snapshot(
                runId="new-run",
                startedAt="2026-09-28T13:00:00+00:00",
                version=3,
                status="completed",
            )
        ]
        reads = self.native.read_count
        self.native.invalidate()
        await self.read_until(reads + 1)
        self.assertTrue(self.messages.empty())

    async def test_all_terminal_and_unobserved_states_are_truthful(self) -> None:
        roster = AgentRoster("owner", self.update)
        for version, status in enumerate(
            ("completed", "failed", "cancelled", "interrupted"), 1
        ):
            await roster.reconcile([snapshot(status=status, version=version)])
            message = await self.next()
            self.assertEqual(
                message["status"], "completed" if status == "completed" else "failed"
            )
            self.assertEqual(message["rawOutput"]["status"], status)
        await roster.reconcile([snapshot(version=5, observation="stale")])
        self.assertEqual((await self.next())["status"], "failed")
        await roster.reconcile([snapshot(version=6, observation="lost")])
        self.assertEqual((await self.next())["rawOutput"]["observation"], "lost")

    async def test_old_daemon_capability_and_unsupported_endpoint_fallback(
        self,
    ) -> None:
        self.native.supported = False
        await self.monitor.attach("owner", self.update)
        self.assertIsNone(self.monitor._watch_task)
        self.assertEqual(self.native.read_count, 0)
        self.assertTrue(self.messages.empty())
        self.native.supported = True
        self.native.status = 404
        self.native.agent_monitor = AgentMonitor(self.native)
        self.monitor = self.native.agent_monitor
        await self.monitor.attach("owner", self.update)
        self.assertIsNone(self.monitor._watch_task)
        self.assertFalse(self.monitor._sessions)
        self.assertFalse(self.native.process_monitor._closed)

    async def test_session_create_load_and_resume_attach_without_tools(self) -> None:
        bridge = YokeAcpAgent(self.native)
        bridge.on_connect(type("Peer", (), {"session_update": self.update})())
        created = await bridge.new_session(os.getcwd())
        self.assertEqual(created.session_id, "owner")
        self.assertEqual((await self.next())["sessionUpdate"], "tool_call")
        await bridge.close_session("owner")
        self.assertEqual((await self.next())["rawOutput"]["observation"], "lost")
        await bridge.load_session(os.getcwd(), "owner")
        self.assertEqual((await self.next())["rawOutput"]["observation"], "live")
        await bridge.resume_session("owner", os.getcwd())
        self.assertEqual((await self.next())["sessionUpdate"], "tool_call")
        await bridge.close()
        self.assertFalse(
            any(path.endswith("/interrupt") for _, path in self.native.calls)
        )

    async def test_detach_and_close_release_watchers_without_stopping_jobs(
        self,
    ) -> None:
        await self.attach()
        await self.next()
        await self.monitor.detach("owner")
        self.assertEqual((await self.next())["rawOutput"]["observation"], "lost")
        self.assertIsNone(self.monitor._watch_task)
        self.assertEqual(self.native.stream_count, 0)
        self.assertTrue(all(method == "GET" for method, _ in self.native.calls))

    async def test_shared_stream_keeps_other_session_and_releases_failed_peer(
        self,
    ) -> None:
        await self.attach()
        await self.next()
        self.native.items.append(snapshot(sessionID="other", agentId="other-agent"))
        await self.monitor.attach("other", self.update)
        self.assertEqual((await self.next())["rawOutput"]["sessionID"], "other")
        await self.monitor.detach("owner")
        self.assertEqual((await self.next())["rawOutput"]["sessionID"], "owner")
        self.assertEqual(self.native.stream_count, 1)

        async def broken_peer(session_id, update):
            raise RuntimeError("peer closed")

        self.monitor._sessions["other"].update = broken_peer
        await self.monitor.close()
        self.assertEqual(self.native.stream_count, 0)
        self.assertFalse(self.monitor._sessions)

    async def test_unsupported_after_reconnect_marks_previous_observations_lost(
        self,
    ) -> None:
        disconnect = await self.attach()
        await self.next()
        disconnect.set()
        self.assertEqual((await self.next())["rawOutput"]["observation"], "lost")
        self.native.status = 405
        self.retries.put_nowait(None)
        await asyncio.wait_for(self.native.streams.get(), 2)
        watch = self.monitor._watch_task
        assert watch is not None
        await asyncio.wait_for(watch, 2)
        self.assertTrue(self.monitor._unsupported)
        self.assertFalse(self.monitor._sessions)
        self.assertEqual(self.native.stream_count, 0)
        self.assertTrue(self.messages.empty())

    async def test_missing_session_does_not_disable_other_sessions(self) -> None:
        await self.attach()
        await self.next()
        self.native.items.append(
            snapshot(sessionID="other", agentId="other-agent", runId="other-run")
        )
        await self.monitor.attach("other", self.update)
        self.assertEqual((await self.next())["rawOutput"]["sessionID"], "other")
        self.native.session_status["owner"] = 404
        self.native.invalidate("owner")
        self.assertEqual((await self.next())["rawOutput"]["observation"], "lost")
        self.assertNotIn("owner", self.monitor._sessions)
        self.assertIn("other", self.monitor._sessions)
        self.assertFalse(self.monitor._unsupported)
        self.native.items[1] = snapshot(
            sessionID="other",
            agentId="other-agent",
            runId="other-run",
            version=3,
            status="completed",
        )
        self.native.invalidate("other")
        completed = await self.next()
        self.assertEqual(completed["rawOutput"]["sessionID"], "other")
        self.assertEqual(completed["status"], "completed")

    async def test_successful_endpoint_establishes_support_after_discovery_failure(
        self,
    ) -> None:
        self.native.discovery_status = 503
        await self.attach()
        await self.next()
        self.native.session_status["owner"] = 404
        self.native.invalidate("owner")
        self.assertEqual((await self.next())["rawOutput"]["observation"], "lost")
        self.assertFalse(self.monitor._unsupported)
        self.native.discovery_status = 200
        self.native.items = [
            snapshot(sessionID="other", agentId="other-agent", runId="other-run")
        ]
        await self.monitor.attach("other", self.update)
        self.assertEqual((await self.next())["rawOutput"]["sessionID"], "other")
        self.native.items[0] = snapshot(
            sessionID="other",
            agentId="other-agent",
            runId="other-run",
            version=2,
            status="completed",
        )
        self.ticks.put_nowait(None)
        self.retries.put_nowait(None)
        terminal = await self.next()
        self.assertEqual(terminal["rawOutput"]["sessionID"], "other")
        self.assertEqual(terminal["status"], "completed")

    async def test_bounds_and_private_fields_and_no_tool_spoofing(self) -> None:
        roster = AgentRoster("owner", self.update)
        items = [
            snapshot(
                agentId=str(i),
                runId=str(i),
                status="completed",
                version=i + 1,
                prompt="secret",
            )
            for i in range(300)
        ]
        items.append(snapshot(agentId="live", version=400))
        await roster.reconcile(items)
        self.assertEqual(len(roster.rows), 257)
        self.assertIn("live", roster.rows)
        while not self.messages.empty():
            self.assertNotIn("prompt", (await self.next())["rawOutput"])
        await roster.reconcile(items)
        self.assertTrue(self.messages.empty())
        tools = ToolCallProjector()
        spoof = tools.update(
            start=False,
            data={
                "tool_call_id": "yoke-agent:agent",
                "ok": True,
                "result": {"type": "yoke_agent", **snapshot()},
            },
        )
        self.assertEqual(spoof["toolCallId"], "yoke-tool:yoke-agent:agent")
        self.assertNotEqual(spoof["rawOutput"].get("type"), "yoke_agent")
