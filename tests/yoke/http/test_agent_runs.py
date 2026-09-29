"""Agent observations use a temporary journal and fake runtime-owned registries."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient
import pytest

from yoke.http.app import HttpAppSettings, create_app
from yoke.http.services.agent_runs import AgentRunService


def snapshot(run: str = "run", **changes: Any) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "agentId": "agent",
        "runId": run,
        "sessionID": None,
        "parentRunId": None,
        "parentAgentId": None,
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
        "lastToolName": None,
        "errorType": None,
        **changes,
    }


class Registry:
    def __init__(self, *items: dict[str, Any]) -> None:
        self.items = list(items)
        self.callbacks: set[Callable[[], None]] = set()
        self.subscribed = Event()
        self.closed = False

    def snapshots(self) -> list[dict[str, Any]]:
        return deepcopy(self.items)

    def subscribe(self, callback: Callable[[], None]) -> Callable[[], None]:
        self.callbacks.add(callback)
        self.subscribed.set()
        return lambda: self.callbacks.discard(callback)

    def change(self, *items: dict[str, Any]) -> None:
        self.items = list(items)
        for callback in list(self.callbacks):
            callback()

    def close(self) -> None:
        self.closed = True


def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    app = create_app(HttpAppSettings(auth_token="secret", session_directory=tmp_path))
    for owner in ("owner", "other"):
        app.state.session_service.store.save(owner, [], root=tmp_path)
    loaded: list[tuple[str, Any]] = []
    monkeypatch.setattr(
        app.state.runtime_registry, "loaded_runtimes", lambda: list(loaded)
    )
    return app, loaded


def runtime(registry: Registry | None = None) -> Any:
    manager = SimpleNamespace(agent_runs=registry) if registry is not None else None
    host = SimpleNamespace(manager=manager, foreground_running=True)
    host.process_manager = lambda: host.manager
    return host


def test_authenticated_owned_route_and_newest_first(tmp_path, monkeypatch):
    app, loaded = setup(tmp_path, monkeypatch)
    registry = Registry(
        snapshot(),
        snapshot("new", startedAt="2026-09-28T13:00:00+00:00", version=2),
        snapshot("foreign", sessionID="other"),
    )
    loaded.extend(
        [("owner", runtime(registry)), ("other", runtime(Registry(snapshot("other"))))]
    )
    with TestClient(app) as client:
        assert client.get("/api/v1/agent-run?sessionID=owner").status_code == 401
        client.headers["Authorization"] = "Bearer secret"
        assert client.get("/api/v1/agent-run").status_code == 422
        assert client.get("/api/v1/agent-run?sessionID=missing").status_code == 404
        assert (
            client.get(
                "/api/v1/agent-run", params={"sessionID": "../../oops"}
            ).status_code
            == 404
        )
        rows = client.get("/api/v1/agent-run?sessionID=owner").json()["data"]
        assert [row["runId"] for row in rows] == ["new", "run"]
        assert all(row["sessionID"] == "owner" for row in rows)
        other = client.get("/api/v1/agent-run?sessionID=other").json()["data"]
        assert [row["runId"] for row in other] == ["other"]
    assert not registry.callbacks
    assert not registry.closed


def test_lifecycle_journal_recovery_is_lost_not_success(tmp_path, monkeypatch):
    app, loaded = setup(tmp_path, monkeypatch)
    registry = Registry(snapshot())
    loaded.append(("owner", runtime(registry)))
    with TestClient(app):
        service = app.state.agent_run_service
        assert service.snapshots("owner")[0]["status"] == "running"
        registry.change(snapshot(version=2, lastToolName="read"))
        # Heartbeats update the read view but never append journal records.
        seq = app.state.event_journal.latest_sequence("owner")
        registry.change(
            snapshot(
                version=2, lastToolName="read", lastSeenAt="2026-09-28T12:00:10+00:00"
            )
        )
        assert service.snapshots("owner")[0]["lastSeenAt"].endswith("10+00:00")
        assert app.state.event_journal.latest_sequence("owner") == seq
        registry.change(
            snapshot("done", agentId="completed-agent", version=3, status="completed"),
            snapshot(version=2, lastToolName="read"),
        )
        before = app.state.event_journal.latest_sequence("owner")
    # A new service instance models a daemon restart. No loaded manager survives.
    loaded.clear()
    recovered = AgentRunService(app.state.runtime_registry, app.state.event_service)
    rows = {row["runId"]: row for row in recovered.snapshots("owner")}
    assert rows["run"]["status"] == "running"
    assert rows["run"]["observation"] == "lost"
    assert rows["run"]["finishedAt"] is None
    recovered_version = rows["run"]["version"]
    completed_version = rows["done"]["version"]
    assert type(recovered_version) is int
    assert type(completed_version) is int
    assert recovered_version > completed_version
    assert rows["done"]["status"] == "completed"
    assert app.state.event_journal.latest_sequence("owner") == before + 1
    assert recovered.snapshots("owner") == list(
        sorted(rows.values(), key=lambda row: str(row["startedAt"]), reverse=True)
    )
    recovered.close()
    again = AgentRunService(app.state.runtime_registry, app.state.event_service)
    assert {row["runId"]: row for row in again.snapshots("owner")} == rows
    assert app.state.event_journal.latest_sequence("owner") == before + 1
    again.close()


def test_manager_created_during_turn_is_observed_after_turn_without_reads(
    tmp_path, monkeypatch
):
    app, loaded = setup(tmp_path, monkeypatch)
    host = runtime()
    loaded.append(("owner", host))
    registry = Registry(snapshot())
    published = Event()
    original = app.state.event_service.durable

    def durable(*args, **kwargs):
        result = original(*args, **kwargs)
        if args[1] == "session.agent.updated":
            published.set()
        return result

    monkeypatch.setattr(app.state.event_service, "durable", durable)
    with TestClient(app):
        host.manager = SimpleNamespace(agent_runs=registry)
        assert registry.subscribed.wait(3)
        assert published.wait(3)
        host.foreground_running = False
        registry.change(snapshot(status="completed", version=2))
        history, _ = app.state.event_journal.history("owner")
        assert [event.data["snapshot"]["status"] for event in history] == [
            "running",
            "completed",
        ]
        assert all(event.type == "session.agent.updated" for event in history)
        assert all(event.session_id == "owner" for event in history)
    assert not registry.callbacks
    assert not registry.closed


def test_duplicates_out_of_order_and_private_fields_never_persist(
    tmp_path, monkeypatch
):
    app, loaded = setup(tmp_path, monkeypatch)
    registry = Registry(
        snapshot(
            prompt="private prompt",
            credentials={"token": "private"},
            output="x" * 100000,
        )
    )
    loaded.append(("owner", runtime(registry)))
    with TestClient(app):
        service = app.state.agent_run_service
        rows = service.snapshots("owner")
        assert not {"prompt", "credentials", "output"} & rows[0].keys()
        registry.change(snapshot(status="completed", version=3))
        registry.change(snapshot(status="running", version=2))
        registry.change(snapshot(status="completed", version=3))
        assert service.snapshots("owner")[0]["status"] == "completed"
        assert app.state.event_journal.latest_sequence("owner") == 2
        content = (tmp_path / "events" / "owner.jsonl").read_text()
        assert "private" not in content
        assert "credentials" not in content
        registry.change(
            snapshot("too-big", name="x" * 257),
            snapshot("wrong-version", schemaVersion=2),
        )
        assert len(service.snapshots("owner")) == 1


def test_completed_retention_never_evicts_live_work(tmp_path, monkeypatch):
    app, loaded = setup(tmp_path, monkeypatch)
    registry = Registry(
        snapshot("live"),
        *(
            snapshot(str(i), agentId=str(i), status="completed", version=i + 2)
            for i in range(300)
        ),
    )
    loaded.append(("owner", runtime(registry)))
    with TestClient(app):
        rows = app.state.agent_run_service.snapshots("owner")
        assert len(rows) == 257
        assert any(row["runId"] == "live" for row in rows)


def test_observer_loss_cannot_consume_the_next_native_completion_revision(
    tmp_path, monkeypatch
):
    app, loaded = setup(tmp_path, monkeypatch)
    registry = Registry(snapshot())
    host = runtime(registry)
    loaded.append(("owner", host))
    with TestClient(app):
        service = app.state.agent_run_service
        first = service.snapshots("owner")[0]
        loaded.clear()
        service.reconcile()
        lost = service.snapshots("owner")[0]
        assert lost["observation"] == "lost"
        assert lost["version"] > first["version"]
        # The native run finishes while the HTTP observer is detached. Its
        # native version can collide with the observer's loss revision.
        registry.change(snapshot(version=2, status="completed"))
        loaded.append(("owner", host))
        terminal = service.snapshots("owner")[0]
        assert terminal["status"] == "completed"
        assert terminal["observation"] == "live"
        assert terminal["version"] > lost["version"]
        registry.change(snapshot(version=1, status="running"))
        assert service.snapshots("owner")[0] == terminal


def test_reconnected_registry_can_restore_same_native_revision(tmp_path, monkeypatch):
    app, loaded = setup(tmp_path, monkeypatch)
    registry = Registry(snapshot())
    host = runtime(registry)
    loaded.append(("owner", host))
    with TestClient(app):
        service = app.state.agent_run_service
        service.snapshots("owner")
        loaded.clear()
        service.reconcile()
        lost = service.snapshots("owner")[0]
        loaded.append(("owner", host))
        restored = service.snapshots("owner")[0]
        assert restored["status"] == "running"
        assert restored["observation"] == "live"
        assert restored["version"] > lost["version"]
        assert service.snapshots("owner")[0] == restored
        seq = app.state.event_journal.latest_sequence("owner")
        app.state.agent_run_service.reconcile()
        assert app.state.event_journal.latest_sequence("owner") == seq


def test_real_core_registry_and_broker_then_fresh_app_restart(tmp_path, monkeypatch):
    from yoke.agent.tools.command_process_manager import CommandProcessManager
    from yoke.agent_runs.records import Owner

    app, loaded = setup(tmp_path, monkeypatch)
    manager = CommandProcessManager()
    host = SimpleNamespace(process_manager=lambda: manager)
    loaded.append(("owner", host))
    token = manager.agent_runs.capability(Owner(runtime_session_id=7))
    with TestClient(app):
        app.state.agent_run_service.reconcile()
        manager.agent_runs.dispatch(
            {
                "op": "register",
                "token": token,
                "runId": "actual-run",
                "agentId": "actual-agent",
                "provider": "fixture",
                "model": "model",
                "name": "Core registry",
            }
        )
        rows = app.state.agent_run_service.snapshots("owner")
        assert rows[0]["sessionID"] == "owner"
        assert rows[0]["runtimeSessionID"] == 7

        async def observe_close():
            subscription = app.state.event_broker.subscribe()
            try:
                # Closing observation is not proof of execution failure.
                manager.agent_runs.close()
                event = await asyncio.wait_for(subscription.queue.get(), 2)
                assert event.type == "session.agent.updated"
                assert event.session_id == "owner"
                assert event.data["snapshot"]["observation"] == "lost"
                assert event.data["snapshot"]["status"] == "running"
            finally:
                app.state.event_broker.unsubscribe(subscription)

        asyncio.run(observe_close())
    manager.close()
    restarted = create_app(
        HttpAppSettings(auth_token="secret", session_directory=tmp_path)
    )
    with TestClient(restarted, headers={"Authorization": "Bearer secret"}) as client:
        recovered = client.get("/api/v1/agent-run?sessionID=owner").json()["data"]
        assert recovered[0]["runId"] == "actual-run"
        assert recovered[0]["status"] == "running"
        assert recovered[0]["observation"] == "lost"
