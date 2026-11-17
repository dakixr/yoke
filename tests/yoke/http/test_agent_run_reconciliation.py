"""Lossy registry notifications cannot leave fabricated live agent state."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from yoke.acp.observation.agents import AgentRoster
from yoke.agent.models import Message
from yoke.agent_runs.registry import AgentRunRegistry
from yoke.ai import Agent, RunConfig

from tests.yoke.agent_tracking_worker import FixtureProvider
from tests.yoke.http.test_agent_runs import setup


def test_terminal_eviction_between_observations_retires_live_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, loaded = setup(tmp_path, monkeypatch)
    registry = AgentRunRegistry(history_limit=1)
    # Deliberately miss notifications. A periodic full snapshot must repair
    # the result even when the actual terminal row has left core retention.
    monkeypatch.setattr(registry, "subscribe", lambda callback: lambda: None)
    loaded.append(
        (
            "owner",
            SimpleNamespace(
                process_manager=lambda: SimpleNamespace(agent_runs=registry)
            ),
        )
    )
    service = app.state.agent_run_service

    class ObservedProvider(FixtureProvider):
        def complete(
            self, messages: list[Message], tools: list[dict[str, object]]
        ) -> Message:
            assert service.snapshots("owner")[0]["status"] == "running"
            return Message.assistant("finished")

    try:
        with registry.bind(session_id="owner"):
            one = Agent(
                provider=ObservedProvider("one"),
                config=RunConfig(root=tmp_path, name="one"),
            )
            two = Agent(
                provider=FixtureProvider("two"),
                config=RunConfig(root=tmp_path, name="two"),
            )
            try:
                one.prompt("one")
                two.prompt("two")
            finally:
                one.close()
                two.close()
        assert len(registry.snapshots()) == 1
        rows = {row["name"]: row for row in service.snapshots("owner")}
        assert rows["one"]["status"] == "running", (
            "No retained evidence proves its outcome"
        )
        assert rows["one"]["observation"] == "lost"
        assert rows["two"]["status"] == "completed"
        sequence = app.state.event_journal.latest_sequence("owner")
        assert service.snapshots("owner")
        assert app.state.event_journal.latest_sequence("owner") == sequence
    finally:
        service.close()
        registry.close()


def test_unicode_exception_name_does_not_discard_real_terminal_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, loaded = setup(tmp_path, monkeypatch)
    registry = AgentRunRegistry()
    monkeypatch.setattr(registry, "subscribe", lambda callback: lambda: None)
    loaded.append(
        (
            "owner",
            SimpleNamespace(
                process_manager=lambda: SimpleNamespace(agent_runs=registry)
            ),
        )
    )
    service = app.state.agent_run_service
    failure = type("Échec", (RuntimeError,), {})

    class FailedProvider(FixtureProvider):
        def complete(
            self, messages: list[Message], tools: list[dict[str, object]]
        ) -> Message:
            assert service.snapshots("owner")[0]["status"] == "running"
            raise failure("Private exception detail")

    agent = Agent(
        provider=FailedProvider("unicode"), config=RunConfig(root=tmp_path, tools=[])
    )
    try:
        with registry.bind(session_id="owner"), pytest.raises(failure):
            agent.prompt("Private prompt")
        raw = registry.snapshots()[0]
        assert raw["status"] == "failed" and raw["errorType"] == "Échec"
        rows = service.snapshots("owner")
        assert rows[0]["status"] == "failed"
        assert rows[0]["observation"] == "live"
        assert rows[0]["errorType"] is None, (
            "Unsupported optional label must not hide the outcome"
        )

        async def project() -> None:
            updates = []

            async def update(owner, payload):
                assert owner == "owner"
                updates.append(payload)

            roster = AgentRoster("owner", update)
            await roster.reconcile(rows)
            assert updates[0]["rawOutput"]["status"] == "failed"
            assert updates[0]["status"] == "failed"
            assert "Private" not in str(updates)

        asyncio.run(project())
    finally:
        agent.close()
        service.close()
        registry.close()


def test_lost_rows_are_retention_terminal_in_the_http_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from yoke.http.services.agent_runs import service as agent_run_service

    monkeypatch.setattr(agent_run_service, "RETAINED_FINISHED", 1)
    rows: dict[str, dict[str, object]] = {
        "lost": {
            "runId": "lost",
            "status": "running",
            "observation": "lost",
            "startedAt": "2026-01-01T00:00:00+00:00",
        },
        "done": {
            "runId": "done",
            "status": "completed",
            "observation": "live",
            "startedAt": "2026-01-02T00:00:00+00:00",
        },
        "live": {
            "runId": "live",
            "status": "running",
            "observation": "live",
            "startedAt": "2026-01-03T00:00:00+00:00",
        },
    }

    agent_run_service.AgentRunService._trim(rows)

    assert set(rows) == {"done", "live"}
