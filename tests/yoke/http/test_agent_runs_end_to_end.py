"""Real SDK subprocesses through native HTTP state and ACP observation projection."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
from typing import Any

from fastapi.testclient import TestClient
import httpx
import pytest

from yoke.acp.native import NativeClient
from yoke.agent.loop.agent import RuntimeAgent
from yoke.agent.models import Message, ToolCall, ToolFunction
from yoke.agent.tools.command import ExecCommandTool
from yoke.http.app import HttpAppSettings, create_app

from tests.yoke.agent_tracking_worker import FixtureProvider
from tests.yoke.test_agent_tracking_integration import (
    WORKER,
    RegistryReceipt,
    mixed_is_ready,
)


class LaunchProvider(FixtureProvider):
    def __init__(self) -> None:
        super().__init__("foreground")
        self.calls = 0

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        self.calls += 1
        if self.calls == 1:
            response = Message.assistant("")
            response.tool_calls = [
                ToolCall(
                    id="launch-agents",
                    function=ToolFunction(
                        name="command_exec",
                        arguments=json.dumps(
                            {
                                "argv": [sys.executable, str(WORKER)],
                                "mode": "background",
                            }
                        ),
                    ),
                )
            ]
            return response
        assert any(message.role == "tool" for message in messages)
        return Message.assistant("Foreground is done; child agents continue.")


def test_background_agents_reach_http_and_acp_after_foreground_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("YOKE_USAGE_METRIC_LOG_DIR", str(tmp_path / "usage"))
    provider = LaunchProvider()
    app = create_app(
        HttpAppSettings(
            auth_token="test-token",
            session_directory=tmp_path / "sessions",
            agent_factory=lambda _record: RuntimeAgent(
                provider,
                [ExecCommandTool.bind(root=tmp_path)],
                tool_root=tmp_path,
            ),
        )
    )
    with TestClient(app, headers={"Authorization": "Bearer test-token"}) as client:
        created = client.post(
            "/api/v1/session",
            json={
                "id": "owner",
                "title": "Fixture",
                "location": {"directory": str(tmp_path)},
            },
        )
        assert created.status_code == 200, created.text
        admitted = client.post(
            "/api/v1/session/owner/prompt", json={"prompt": {"text": "Launch"}}
        )
        assert admitted.status_code == 200, admitted.text
        waited = client.post("/api/v1/session/owner/wait", params={"timeoutMs": 20000})
        assert waited.status_code == 200, waited.text
        assert waited.json()["data"]["state"] == "idle", waited.text
        runtime = app.state.runtime_registry.get_if_loaded("owner")
        manager = runtime.process_manager()
        assert manager is not None
        receipt = RegistryReceipt(manager)
        try:
            ready = receipt.wait(mixed_is_ready)
            process_id = ready[0]["runtimeSessionID"]
            assert type(process_id) is int
            response = client.get("/api/v1/agent-run", params={"sessionID": "owner"})
            assert response.status_code == 200, response.text
            assert mixed_is_ready(response.json()["data"])

            async def observe() -> None:
                native = NativeClient("http://fixture", "test-token")
                await native.http.aclose()
                native.http = httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://fixture/api/v1/",
                    headers={"Authorization": "Bearer test-token"},
                )
                outputs: asyncio.Queue[dict[str, Any]] = asyncio.Queue()

                async def events(
                    queue: asyncio.Queue[dict[str, Any]], connected: asyncio.Event
                ) -> None:
                    # Use the real native broker. Only network SSE framing is replaced.
                    subscription = app.state.event_broker.subscribe()
                    connected.set()
                    try:
                        while True:
                            event = await subscription.queue.get()
                            assert event is not None
                            queue.put_nowait(event.model_dump(by_alias=True))
                    finally:
                        app.state.event_broker.unsubscribe(subscription)

                async def update(owner: str, payload: dict[str, Any]) -> None:
                    assert owner == "owner"
                    await outputs.put(payload)

                monkeypatch.setattr(native, "events", events)
                try:
                    await native.agent_monitor.attach("owner", update)
                    initial = [
                        await asyncio.wait_for(outputs.get(), 10) for _ in range(3)
                    ]
                    assert {row["rawOutput"]["name"] for row in initial} == {
                        "success",
                        "failed",
                        "waiting",
                    }
                    waiting = next(
                        row for row in initial if row["rawOutput"]["name"] == "waiting"
                    )
                    assert waiting["status"] == "in_progress"
                    assert waiting["rawOutput"]["type"] == "yoke_agent"
                    assert (
                        waiting["toolCallId"]
                        == "yoke-agent:" + waiting["rawOutput"]["agentId"]
                    )
                    assert "SECRET_" not in json.dumps(initial)
                    # No foreground prompt or process_read drives this transition.
                    manager.write_input(process_id, "release\n")
                    while True:
                        update_row = await asyncio.wait_for(outputs.get(), 15)
                        if (
                            update_row["rawOutput"]["name"] == "waiting"
                            and update_row["rawOutput"]["status"] == "completed"
                        ):
                            assert update_row["sessionUpdate"] == "tool_call_update"
                            assert update_row["status"] == "completed"
                            break
                finally:
                    await native.close()

            asyncio.run(observe())
            final = receipt.wait(
                lambda rows: (
                    len(rows) == 4 and all(row["status"] != "running" for row in rows)
                )
            )
            assert len(final) == 4
            retained = client.get(
                "/api/v1/agent-run", params={"sessionID": "owner"}
            ).json()["data"]
            assert len(retained) == 4
            assert "SECRET_" not in json.dumps(retained)
        finally:
            receipt.unsubscribe()
