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

from yoke.http.app import HttpAppSettings, create_app
from yoke.http.routes.event import stream_events
from yoke.http.runtime.execution import _PUBLIC_AGENT_EVENTS
from yoke.http.runtime.turns import TurnExecution
from yoke.session.admissions import AdmissionRecord
from tests.yoke.http.test_runtime_lifetime_regressions import (
    TOKEN,
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
