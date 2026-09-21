"""ACP adapter tests using a mock native Yoke HTTP runtime."""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from typing import Any

import httpx
from acp import RequestError
from acp import schema as s

from yoke.acp.bridge import YokeAcpAgent
from yoke.acp.native import NativeClient


class FixtureNative(NativeClient):
    def __init__(self) -> None:
        super().__init__("http://fixture", "test-secret")
        self.http = httpx.AsyncClient(
            base_url="http://fixture/api/v1/",
            transport=httpx.MockTransport(self.respond),
        )
        self.calls: list[tuple[str, str]] = []
        self.queue: asyncio.Queue[dict[str, Any]] | None = None
        self.admission: dict[str, Any] | None = None
        self.running = False
        self.hold = False
        self.interrupted = False
        self.admitted = asyncio.Event()
        self.drain_started = asyncio.Event()
        self.drain_release = asyncio.Event()
        self.drain_release.set()
        self.selection = {
            "provider": "fixture",
            "model": "model",
            "reasoningEffort": "high",
        }

    async def events(
        self,
        queue: asyncio.Queue[dict[str, Any]],
        ready: asyncio.Event,
    ) -> None:
        self.queue = queue
        ready.set()
        await asyncio.Event().wait()

    async def respond(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1/")
        self.calls.append((request.method, path))
        session = {
            "id": "native-id",
            "location": {"directory": os.getcwd()},
            "selection": self.selection,
            "queue": {"total": 0},
        }
        if path == "provider":
            data = [
                {
                    "id": "fixture",
                    "ready": True,
                    "currentModel": "model",
                    "currentReasoningEffort": "high",
                }
            ]
        elif path == "model":
            data = [
                {
                    "id": "model",
                    "provider": "fixture",
                    "name": "Fixture model",
                    "reasoningEfforts": ["low", "high"],
                    "capabilities": {"images": True},
                    "contextWindowTokens": 123,
                },
                {
                    "id": "other",
                    "provider": "fixture",
                    "name": "Other model",
                    "reasoningEfforts": ["low"],
                    "capabilities": {"images": False},
                },
            ]
        elif path == "openapi.json":
            return httpx.Response(
                200,
                json={"paths": {"/api/v1/session/{session_id}/drain": {"post": {}}}},
            )
        elif path == "skill":
            return httpx.Response(
                200,
                json={
                    "location": {"directory": request.url.params["directory"]},
                    "data": [
                        {
                            "name": "review",
                            "description": "Review the change.",
                            "sourcePath": "/skills/review/SKILL.md",
                            "active": False,
                        }
                    ],
                },
            )
        elif path in {"session", "session/native-id"}:
            data = session
        elif path == "session/active":
            data = {}
        elif path.endswith("/selection"):
            self.selection = json.loads(request.content)
            data = {}
        elif path.endswith("/wait"):
            data = {"state": "running" if self.running else "idle"}
        elif path.endswith("/drain"):
            if self.running and self.interrupted:
                self.drain_started.set()
                await self.drain_release.wait()
                self.running = False
            data = {
                "drained": not self.running,
                "activeWorkers": 1 if self.running else 0,
            }
        elif path.endswith("/interrupt"):
            self.interrupted = True
            data = {"interrupted": True}
        elif path.endswith("/prompt"):
            admission: dict[str, Any] = json.loads(request.content)
            self.admission = admission
            self.admitted.set()
            self.running = self.hold
            if self.queue is None:
                raise AssertionError("event stream was not ready")
            for kind, values in [
                (
                    "session.tool.started",
                    {
                        "tool_call_id": "call",
                        "tool_name": "read",
                        "tool_arguments": {},
                    },
                ),
                (
                    "session.tool.ended",
                    {"tool_call_id": "call", "ok": True, "result": {"ok": True}},
                ),
            ]:
                self.queue.put_nowait(
                    {
                        "type": kind,
                        "sessionID": "native-id",
                        "data": {"inputID": admission["id"], **values},
                    }
                )
            data = {"id": admission["id"], "admittedSeq": 0}
        elif path.endswith("/history"):
            events = (
                []
                if self.hold or self.admission is None
                else [
                    {
                        "type": "session.prompt.settled",
                        "durable": {"seq": 1},
                        "data": {
                            "inputID": self.admission["id"],
                            "outcome": "completed",
                        },
                    }
                ]
            )
            return httpx.Response(200, json={"data": events, "hasMore": False})
        elif path.endswith("/message"):
            data = (
                []
                if self.admission is None
                else [
                    {
                        "type": "user",
                        "inputID": self.admission["id"],
                        "content": [],
                    },
                    {
                        "type": "assistant",
                        "phase": "final_answer",
                        "content": [{"type": "text", "text": "Done."}],
                    },
                ]
            )
            return httpx.Response(200, json={"data": data, "cursor": {"next": None}})
        else:
            raise AssertionError(path)
        return httpx.Response(200, json={"data": data})


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.native = FixtureNative()
        self.agent = YokeAcpAgent(self.native)
        self.updates: list[dict[str, Any]] = []
        self.agent.conn = self
        await self.agent.new_session(os.getcwd())

    async def asyncTearDown(self) -> None:
        await self.agent.close()

    async def session_update(self, session_id: str, update: dict[str, Any]) -> None:
        s.SessionNotification.model_validate(
            {"sessionId": session_id, "update": update}
        )
        self.updates.append(update)

    async def test_initialize_exposes_native_catalog_and_worker_drain(self) -> None:
        result = (
            await self.agent.initialize(
                1,
                s.ClientCapabilities.model_validate(
                    {"_meta": {"yoke": {"cwd": os.getcwd()}}}
                ),
            )
        ).model_dump(by_alias=True)
        meta = result["_meta"]["yoke"]
        self.assertTrue(meta["ready"])
        self.assertTrue(meta["workerDrain"])
        self.assertEqual(meta["defaultModel"], "fixture:model")
        self.assertEqual(
            [model["id"] for model in meta["models"]],
            ["fixture:model", "fixture:other"],
        )
        self.assertEqual(
            meta["skills"],
            [
                {
                    "name": "review",
                    "description": "Review the change.",
                    "path": "/skills/review/SKILL.md",
                    "enabled": True,
                }
            ],
        )
        self.assertIn(("GET", "skill"), self.native.calls)

    async def test_prompt_emits_tool_lifecycle_and_final_text(self) -> None:
        result = await self.agent.prompt(
            "native-id",
            [s.TextContentBlock(type="text", text="exact")],
            yokeInputID="stable-id",
        )
        self.assertEqual(result.stop_reason, "end_turn")
        admission = self.native.admission
        self.assertIsNotNone(admission)
        assert admission is not None
        self.assertEqual(admission["prompt"]["text"], "exact")
        self.assertEqual(
            [update["sessionUpdate"] for update in self.updates],
            ["tool_call", "tool_call_update", "agent_message_chunk"],
        )
        self.assertEqual(self.updates[-1]["content"]["text"], "Done.")

    async def test_cancel_waits_for_native_worker_drain_and_can_resume(self) -> None:
        self.native.hold = True
        self.native.drain_release.clear()
        task = asyncio.create_task(
            self.agent.prompt(
                "native-id", [s.TextContentBlock(type="text", text="stop")]
            )
        )
        await self.native.admitted.wait()
        cancel = asyncio.create_task(self.agent.cancel("native-id"))
        await self.native.drain_started.wait()
        self.assertFalse(cancel.done())
        self.native.drain_release.set()
        await cancel
        self.assertEqual((await task).stop_reason, "cancelled")
        self.assertTrue(self.native.interrupted)
        self.assertFalse(self.native.running)
        restarted = YokeAcpAgent(self.native)
        restarted.conn = self
        await restarted.resume_session("native-id", os.getcwd())
        self.assertIn("native-id", restarted.sessions)

    async def test_selection_validation_and_workspace_identity(self) -> None:
        await self.agent.set_config_option("model", "native-id", "fixture:other")
        self.assertEqual(self.native.selection["model"], "other")
        with self.assertRaisesRegex(RequestError, "reasoning"):
            await self.agent.set_config_option("reasoningEffort", "native-id", "high")
        with self.assertRaisesRegex(RequestError, "cwd mismatch"):
            await self.agent.resume_session("native-id", "/")


def test_native_errors_do_not_echo_response_bodies() -> None:
    async def scenario() -> None:
        native = NativeClient("http://fixture", "test-secret")
        await native.http.aclose()
        native.http = httpx.AsyncClient(
            base_url="http://fixture/api/v1/",
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(401, json={"error": "do-not-leak"})
            ),
        )
        try:
            with unittest.TestCase().assertRaisesRegex(
                RequestError, "Native HTTP error 401"
            ) as error:
                await native.data("GET", "session/active")
            assert "do-not-leak" not in str(error.exception)
        finally:
            await native.close()

    asyncio.run(scenario())
