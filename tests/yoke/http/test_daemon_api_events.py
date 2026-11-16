"""Session-filtered SSE, CLI renderer payloads and visible admission capacity."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import ExitStack
from threading import Event
from time import monotonic

import pytest

from fastapi import Request
from fastapi.testclient import TestClient

from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import Message
from yoke.http.app import HttpAppSettings, create_app
from yoke.http.routes.event import stream_events
from yoke.http.runtime.execution import _PUBLIC_AGENT_EVENTS
from yoke.http.runtime.turns import TurnExecution
from yoke.http.services.event_broker import live_event
from yoke.session.admissions import AdmissionRecord
from tests.yoke.http.test_runtime_lifetime_regressions import (
    TOKEN,
    _auth,
    _create_session,
)


@pytest.fixture(autouse=True)
def api_environment_sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("YOKE_DB_PATH", str(tmp_path / "sessions" / "yoke.db"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))


async def open_stream(app, session_id) -> AsyncGenerator[str, None]:
    request = Request({"type": "http", "app": app})
    stream = await stream_events(request, session_id=session_id, client=None)
    body = stream.body_iterator
    assert isinstance(body, AsyncGenerator)

    async def strings() -> AsyncGenerator[str, None]:
        try:
            async for chunk in body:
                assert isinstance(chunk, str)
                yield chunk
        finally:
            await body.aclose()

    iterator = strings()
    assert "server.connected" in await anext(iterator)
    return iterator


def test_filtered_streams_are_isolated_before_bounded_fanout(tmp_path):
    app = create_app(
        HttpAppSettings(auth_token=TOKEN, session_directory=tmp_path / "sessions")
    )
    with TestClient(app) as client:
        _create_session(client, tmp_path, "one")
        _create_session(client, tmp_path, "two")

        async def check():
            one = await open_stream(app, "one")
            two = await open_stream(app, "two")
            global_stream = await open_stream(app, None)
            try:
                app.state.event_service.live(
                    "session.notice", {"text": "first"}, session_id="one"
                )
                app.state.event_service.live(
                    "session.notice", {"text": "second"}, session_id="two"
                )
                assert '"sessionID":"one"' in await anext(global_stream)
                assert '"sessionID":"two"' in await anext(global_stream)
                assert '"sessionID":"one"' in await anext(one)
                assert '"sessionID":"two"' in await anext(two)
                await global_stream.aclose()
                for index in range(600):
                    app.state.event_broker.publish(
                        live_event(
                            "session.warning", {"index": index}, session_id="two"
                        ),
                        ephemeral=True,
                    )
                app.state.event_service.durable(
                    "one", "session.updated", {"title": "mine"}
                )
                own = await asyncio.wait_for(anext(one), 2)
                assert '"sessionID":"one"' in own
                assert '"title":"mine"' in own
                assert "resyncRequired" not in own
                other = await asyncio.wait_for(anext(two), 2)
                assert '"sessionID":"two"' in other
                app.state.event_broker.publish(live_event("server.stopping", {}))
                assert "server.stopping" in await anext(one)
            finally:
                await one.aclose()
                await two.aclose()
                await global_stream.aclose()

        asyncio.run(check())


def test_all_cli_agent_event_payloads_reach_filtered_sse(tmp_path):
    assert {
        "model_start",
        "model_end",
        "provider_retry",
        "provider_rate_limited",
        "provider_recovered",
        "notice",
        "warning",
        "error",
        "compaction_summary_start",
        "compaction_summary_end",
        "context_compaction",
        "tool_execution_output_delta",
    } <= _PUBLIC_AGENT_EVENTS.keys()
    app = create_app(
        HttpAppSettings(auth_token=TOKEN, session_directory=tmp_path / "sessions")
    )
    with TestClient(app) as client:
        _create_session(client, tmp_path, "one")

        async def check():
            runtime = app.state.runtime_registry.get_or_start("one")
            execution = TurnExecution(
                turn_id=7,
                admission=AdmissionRecord(
                    id="input",
                    session_id="one",
                    prompt="hello",
                    delivery="steer",
                    fingerprint="f",
                    time_created="now",
                    admitted_seq=1,
                ),
                started_at="now",
                started_monotonic=monotonic(),
                stop_event=Event(),
                retired_event=Event(),
                cold_start=False,
                automatic_title=False,
                workspace_use=ExitStack(),
            )
            runtime.activity.active = execution
            iterator = await open_stream(app, "one")
            try:
                for name, public_name in _PUBLIC_AGENT_EVENTS.items():
                    payload = {
                        "content": "hello",
                        "input_tokens": 23,
                        "error": "retry detail",
                        "provider_result_projection": {"private": True},
                    }
                    runtime.execution.publish(execution, name, payload, None)
                    while True:
                        data = await asyncio.wait_for(anext(iterator), 2)
                        if f"event: {public_name}\n" in data:
                            break
                    assert '"content":"hello"' in data, name
                    assert '"input_tokens":23' in data, name
                    if name != "context_usage":
                        assert '"turnID":7' in data
                        assert '"inputID":"input"' in data
                    assert "provider_result_projection" not in data
            finally:
                runtime.activity.active = None
                await iterator.aclose()

        asyncio.run(check())


class WaitingProvider:
    def __init__(self, started, release):
        self.started = started
        self.release = release

    def fork_for_turn(self):
        return WaitingProvider(self.started, self.release)

    def complete(self, messages, tools):
        self.started.set()
        assert self.release.wait(10)
        return Message.assistant("finished")


def test_over_capacity_admission_waits_and_eventually_runs(tmp_path):
    started, release = Event(), Event()
    app = create_app(
        HttpAppSettings(
            auth_token=TOKEN,
            session_directory=tmp_path / "sessions",
            max_active_sessions=1,
            agent_factory=lambda record: RuntimeAgent(
                WaitingProvider(started, release), []
            ),
        )
    )
    with TestClient(app) as client:
        for name in ("one", "two"):
            _create_session(client, tmp_path, name)
            client.patch(
                f"/api/v1/session/{name}", headers=_auth(), json={"title": name}
            ).raise_for_status()
        client.post(
            "/api/v1/session/one/prompt",
            headers=_auth(),
            json={"prompt": {"text": "first"}},
        ).raise_for_status()
        assert started.wait(3), client.get(
            "/api/v1/session/active", headers=_auth()
        ).text
        try:
            client.post(
                "/api/v1/session/two/prompt",
                headers=_auth(),
                json={"prompt": {"text": "second"}},
            ).raise_for_status()
            active = client.get("/api/v1/session/active", headers=_auth()).json()[
                "data"
            ]
            assert active["two"]["state"] == "running"
            assert active["two"]["activity"] == "Waiting for a slot"
        finally:
            release.set()
        for name in ("one", "two"):
            result = client.post(
                f"/api/v1/session/{name}/wait?timeoutMs=5000", headers=_auth()
            ).json()["data"]
            assert result["state"] == "idle"
            messages = client.get(
                f"/api/v1/session/{name}/message", headers=_auth()
            ).text
            assert "finished" in messages
