"""Fast regressions for truthful restart control and bounded discovery."""

from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from starlette.requests import Request

from yoke.daemon import client
from yoke.daemon.lifecycle import DaemonLifecycle
from yoke.daemon.paths import DaemonPaths
from yoke.http.routes.daemon import daemon_restart
from yoke.session.repository.utils import default_database_path
from yoke.storage.errors import database_open_error
from yoke.storage.schema import DatabaseError, check_schema


def request_for(lifecycle):
    return Request(
        {
            "type": "http",
            "app": SimpleNamespace(state=SimpleNamespace(daemon=lifecycle)),
        }
    )


def test_pending_restart_does_not_claim_now():
    async def scenario():
        lifecycle = DaemonLifecycle()
        response = await daemon_restart(request_for(lifecycle))
        assert response.data.scheduled == "when_idle"
        assert lifecycle.exit_reason is None

    asyncio.run(scenario())


def test_restart_racing_completed_admission_waits():
    from yoke.http.errors import ApiError
    from yoke.http.models.daemon import DaemonRestartRequest

    async def scenario():
        lifecycle = DaemonLifecycle()

        async def probe():
            lifecycle.begin_mutation()
            lifecycle.end_mutation()
            return False

        lifecycle.bind_busy_probe(probe)
        with pytest.raises(ApiError):
            await daemon_restart(request_for(lifecycle))
        response = await daemon_restart(
            request_for(lifecycle), DaemonRestartRequest(when_idle=True)
        )
        assert response.data.scheduled == "when_idle"
        assert lifecycle.exit_reason is None

    asyncio.run(scenario())


@pytest.mark.parametrize("scheduled", ["now", "when_idle"])
def test_restart_timeout_falls_back_to_skew_policy(tmp_path, monkeypatch, scheduled):
    paths = DaemonPaths(tmp_path, tmp_path / "run")
    monkeypatch.setattr(
        client,
        "unix_client",
        lambda *a, **k: httpx.Client(
            base_url="http://yoke/",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200, json={"data": {"scheduled": scheduled}}
                )
            ),
        ),
    )
    wait = Mock(side_effect=client.DaemonUnavailable("still draining"))
    monkeypatch.setattr(client, "wait_for_exit", wait)
    assert not client.restart_daemon(paths.socket, 123, timeout=0, paths=paths)
    wait.assert_called_once()


@pytest.mark.parametrize("restarted", [False, True])
def test_discovery_steps_restart_at_most_once(tmp_path, monkeypatch, restarted):
    paths = DaemonPaths(tmp_path, tmp_path / "run")
    monkeypatch.setattr(client, "find_daemon", lambda paths: paths.socket)
    monkeypatch.setattr(client, "build_id", lambda: "3.40.0+gclient")
    monkeypatch.setattr(client, "fresh_build_id", lambda: "3.40.0+gdisk")
    monkeypatch.setattr(client, "protocol_version", lambda: "2")
    monkeypatch.setattr(
        client,
        "unix_client",
        lambda *a, **k: httpx.Client(
            base_url="http://yoke/",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "data": {
                            "pid": 123,
                            "buildID": "3.38.0+gold",
                            "protocolVersion": "2",
                            "busy": False,
                            "databasePath": str(default_database_path().resolve()),
                        }
                    },
                )
            ),
        ),
    )
    restart = Mock(return_value=restarted)
    monkeypatch.setattr(client, "restart_daemon", restart)
    steps = []
    monkeypatch.setattr(client, "_discovery_step", steps.append)
    assert client.connect_or_spawn(paths).warning
    restart.assert_called_once()
    assert steps == [
        client.DiscoveryStep.PROBE,
        client.DiscoveryStep.STATUS,
        client.DiscoveryStep.DECIDE,
        client.DiscoveryStep.RESTART_ONCE,
        client.DiscoveryStep.PROBE,
        client.DiscoveryStep.STATUS,
        client.DiscoveryStep.DECIDE,
        client.DiscoveryStep.ATTACH,
    ]


def test_older_schema_only_offers_migration(tmp_path):
    path = tmp_path / "yoke.db"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=1")
        with pytest.raises(DatabaseError) as error:
            check_schema(connection, path)
    message = database_open_error(path, error.value)
    assert "Start the Yoke daemon or run yoke db migrate" in message
    assert "restore" not in message
    assert "db check" not in message
    assert ".." not in message
    damaged = database_open_error(path, sqlite3.DatabaseError("file is not a database"))
    assert "db check" in damaged
    assert "restore" in damaged
    assert ".." not in damaged


def test_status_reports_phase_and_consistent_busy_reasons(tmp_path):
    from yoke.daemon.work import WorkSummary
    from yoke.http.routes.daemon import daemon_status

    async def scenario():
        lifecycle = DaemonLifecycle()

        async def probe():
            return WorkSummary(held_queues=("held",))

        lifecycle.bind_busy_probe(probe)
        lifecycle.begin_mutation()
        lifecycle.request_stop_when_idle()
        request = request_for(lifecycle)
        request.app.state.daemon_socket = "test.sock"
        request.app.state.session_service = SimpleNamespace(
            store=SimpleNamespace(database=SimpleNamespace(path=tmp_path / "yoke.db"))
        )
        status = (await daemon_status(request)).data.model_dump(by_alias=True)
        assert status["phase"] == "draining"
        assert status["pendingExit"] == "stop"
        assert status["exitReason"] is None
        assert not status["restartPending"]
        assert status["busy"]
        assert status["work"] == {
            "runningSessions": [],
            "runnableQueues": [],
            "heldQueues": ["held"],
            "mutations": 1,
            "admissionChanged": False,
        }
        lifecycle.end_mutation()
        assert not (await daemon_status(request)).data.busy

    asyncio.run(scenario())


def test_daemon_status_command_shows_phase_and_busy_reasons(tmp_path, monkeypatch):
    import typer
    from typer.testing import CliRunner
    from yoke.cli.daemon_app import daemon_app

    paths = DaemonPaths(tmp_path, tmp_path / "run")
    monkeypatch.setattr(client, "find_daemon", lambda paths: paths.socket)
    data = {
        "pid": 123,
        "buildID": "3.40.0",
        "protocolVersion": "2",
        "port": None,
        "socket": str(paths.socket),
        "idleExit": True,
        "busy": True,
        "restartPending": True,
        "phase": "draining",
        "pendingExit": "restart",
        "leases": [],
        "work": {
            "runningSessions": ["active"],
            "runnableQueues": ["queued"],
            "heldQueues": ["held"],
            "mutations": 2,
            "admissionChanged": True,
        },
    }
    monkeypatch.setattr(
        client,
        "unix_client",
        lambda *a, **k: httpx.Client(
            base_url="http://yoke/",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"data": data})
            ),
        ),
    )
    app = typer.Typer()
    app.add_typer(daemon_app, name="daemon")
    result = CliRunner().invoke(app, ["daemon", "status"])
    assert result.exit_code == 0, result.output
    assert "draining (restart)" in result.output
    assert "active    active" in result.output
    assert "queued    queued" in result.output
    assert "held      held" in result.output
    assert "mutations 2" in result.output
    assert "admission changed during probe" in result.output
