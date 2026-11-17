"""Agent observations use a temporary journal and fake runtime-owned registries."""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient
import pytest

from yoke.http.app import HttpAppSettings, create_app


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
