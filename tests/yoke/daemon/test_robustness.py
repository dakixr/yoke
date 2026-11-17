"""Regression checks for shared-daemon recovery without subprocess daemons."""

from __future__ import annotations

import asyncio
import sqlite3
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from yoke.daemon import client
from yoke.daemon.paths import DaemonPaths
from yoke.http import app as app_module
from yoke.session.repository.utils import default_database_path


@pytest.mark.parametrize(
    "daemon_build,disk_build,busy,expected",
    [
        ("3.35.0+gdisk", "3.35.0+gdisk", False, 0),
        ("3.34.1+gdaemon", "3.35.0+gdisk", False, 1),
        ("3.34.1+gdaemon", "3.35.0+gdisk", True, 0),
    ],
)
def test_old_client_converges_without_scheduling_restarts(
    tmp_path, monkeypatch, daemon_build, disk_build, busy, expected
):
    monkeypatch.setattr(client, "protocol_version", lambda: "1")
    paths = DaemonPaths(tmp_path, tmp_path / "run")
    restarts = []
    posts = []
    monkeypatch.setenv("YOKE_DAEMON_BUILD_ID", disk_build)
    monkeypatch.setattr(client, "build_id", lambda: "3.34.0+goldclient")
    monkeypatch.setattr(client, "find_daemon", lambda paths: paths.socket)

    def handle(request):
        if request.method == "POST":
            posts.append(request)
        return httpx.Response(
            200,
            json={
                "data": {
                    "pid": 123,
                    "buildID": daemon_build,
                    "protocolVersion": "1",
                    "busy": busy,
                    "databasePath": str(default_database_path().resolve()),
                }
            },
        )

    monkeypatch.setattr(
        client,
        "unix_client",
        lambda *args, **kwargs: httpx.Client(
            transport=httpx.MockTransport(handle), base_url="http://yoke/"
        ),
    )

    def restart(*args, **kwargs):
        nonlocal daemon_build
        restarts.append(daemon_build)
        daemon_build = disk_build
        return True

    monkeypatch.setattr(client, "restart_daemon", restart)
    for _ in range(4):
        attached = client.connect_or_spawn(paths)
        assert attached.warning
    assert len(restarts) == expected
    assert posts == []


@pytest.mark.parametrize(
    "failure", [sqlite3.OperationalError("busy"), ValueError("blob")]
)
def test_maintenance_retries_initial_and_later_failures(monkeypatch, caplog, failure):
    store = Mock()
    calls = []
    delays = []

    def maintain(*, force=False):
        calls.append(force)
        if len(calls) in (1, 2, 4):
            raise failure

    async def sleep(delay):
        delays.append(delay)
        if len(calls) == 6:
            raise asyncio.CancelledError

    store.maintain_index = maintain
    monkeypatch.setattr(app_module.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(app_module._maintain_session_index(store))
    assert calls == [True, True, True, False, False, False]
    assert delays == [5, 10, 5, 5, 5, 5]
    assert caplog.text.count("Session index maintenance failed") == 3


def test_shutdown_cancels_all_watchers_and_closes_runtimes(
    tmp_path, monkeypatch, caplog
):
    async def scenario():
        entered = asyncio.Event()
        cancelled = []

        async def failed():
            raise ValueError("watcher failed")

        async def watch():
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)

        monkeypatch.setattr(
            app_module.ThreadSettlementService, "watch", lambda self: failed()
        )
        monkeypatch.setattr(app_module.AgentRunService, "watch", lambda self: watch())
        app = app_module.create_app(
            app_module.HttpAppSettings(
                auth_token=None, session_directory=tmp_path / "db"
            )
        )
        close = AsyncMock()
        shutdown = Mock()
        monkeypatch.setattr(app.state.runtime_registry, "close_runtimes", close)
        monkeypatch.setattr(app.state.runtime_registry, "shutdown_executor", shutdown)
        async with app.router.lifespan_context(app):
            await entered.wait()
        assert cancelled == [True]
        close.assert_awaited_once()
        shutdown.assert_called_once()

    asyncio.run(scenario())
    assert "watcher failed" in caplog.text


def test_restart_admission_race_is_attempted_only_once(tmp_path, monkeypatch):
    monkeypatch.setattr(client, "protocol_version", lambda: "1")
    paths = DaemonPaths(tmp_path, tmp_path / "run")
    monkeypatch.setenv("YOKE_DAEMON_BUILD_ID", "3.35.0+gdisk")
    monkeypatch.setattr(client, "build_id", lambda: "3.35.0+gclient")
    monkeypatch.setattr(client, "find_daemon", lambda paths: paths.socket)
    posts = []

    def respond(request):
        if request.method == "POST":
            posts.append(request)
        return httpx.Response(
            200,
            json={
                "data": {
                    "pid": 123,
                    "buildID": "3.34.1+gdaemon",
                    "protocolVersion": "1",
                    "busy": False,
                    "databasePath": str(default_database_path().resolve()),
                }
            },
        )

    monkeypatch.setattr(
        client,
        "unix_client",
        lambda *args, **kwargs: httpx.Client(
            base_url="http://yoke/", transport=httpx.MockTransport(respond)
        ),
    )
    # The admission that blocked restart can finish before the next status read.
    # It still must not cause an unbounded recursive restart attempt.
    restart = Mock(return_value=False)
    monkeypatch.setattr(client, "restart_daemon", restart)
    assert client.connect_or_spawn(paths).warning
    restart.assert_called_once()
    assert posts == []
