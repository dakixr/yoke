"""Transport recovery exercises real HTTP/SSE over an isolated Unix socket."""

from concurrent.futures import ThreadPoolExecutor

from tests.yoke.daemon.support import sandbox as sandbox

from yoke.cli.backend.connection import DaemonRequestError, SessionConnection
from yoke.http.app import HttpAppSettings, create_app
from yoke.http.services.event_broker import live_event

from .daemon_support import serve_app


def test_session_stream_recovers_history_without_duplicate_durable_events(
    tmp_path, monkeypatch
):
    app = create_app(
        HttpAppSettings(auth_token=None, session_directory=tmp_path / "db")
    )
    with serve_app(app) as handle:
        connection = SessionConnection("test", discover=lambda: handle, cwd=tmp_path)
        connection.request(
            "POST",
            "session",
            json={"id": "test", "location": {"directory": str(tmp_path)}},
        )
        stream, unsubscribe = connection.events.subscribe()
        try:
            connection.start()
            with ThreadPoolExecutor(max_workers=1) as pool:
                iterator = iter(stream)

                def until(kind):
                    while True:
                        event = next(iterator)
                        if event["type"] == kind:
                            return event

                first = pool.submit(until, "client.resynced").result(timeout=5)
                assert first["payload"]["messages"] == []
                connection.session_request("PATCH", json={"title": "changed"})
                updated = pool.submit(until, "session.updated").result(timeout=5)
                assert updated["payload"]["title"] == "changed"
                app.state.event_broker.publish(live_event("server.resyncRequired", {}))
                # No duplicate session.updated can precede the recovered snapshot.
                event = pool.submit(lambda: next(iterator)).result(timeout=5)
                assert event["type"] == "client.resynced"
                recovered_seq, initial_seq = (
                    event["payload"]["seq"],
                    first["payload"]["seq"],
                )
                assert isinstance(recovered_seq, int) and isinstance(initial_seq, int)
                assert recovered_seq >= initial_seq
                assert connection.session_request("GET", "/client")["data"][
                    "owners"
                ] == [connection.owner]
                clients = app.state.runtime_registry.get_if_loaded(
                    "test"
                ).resources.configuration.clients
                previous = clients.select(connection.owner)
                monkeypatch.setenv("YOKE_TEST_REATTACH", "updated-after-disconnect")
                app.state.event_broker.close()
                recovered = pool.submit(until, "client.resynced").result(timeout=5)
                assert recovered["payload"]["messages"] == []
                attached = clients.select(connection.owner)
                assert attached is not previous
                assert (
                    attached.environment.values["YOKE_TEST_REATTACH"]
                    == "updated-after-disconnect"
                )
                assert attached.environment.cwd == tmp_path
        finally:
            unsubscribe()
            connection.close()
        assert connection.session_request("GET", "/client")["data"] is None


def test_rejected_mutation_is_not_retried(tmp_path):
    app = create_app(
        HttpAppSettings(auth_token=None, session_directory=tmp_path / "db")
    )
    with serve_app(app) as handle:
        connection = SessionConnection("absent", discover=lambda: handle, cwd=tmp_path)
        try:
            connection.session_request("PATCH", json={"title": "changed"})
        except DaemonRequestError as exc:
            assert exc.status == 404
            assert exc.body["error"]["code"] == "session_not_found"
        else:
            raise AssertionError("Missing session mutation succeeded")


def test_restart_reattaches_fresh_environment_and_keeps_history(sandbox, monkeypatch):
    import os
    import signal

    from tests.yoke.daemon.support import eventually

    monkeypatch.setenv("YOKE_DAEMON_IDLE_GRACE", "10")
    connection = SessionConnection("test", cwd=sandbox.root)
    connection.request(
        "POST",
        "session",
        json={
            "id": "test",
            "title": "Existing conversation",
            "location": {"directory": str(sandbox.root)},
        },
    )
    stream, unsubscribe = connection.events.subscribe()
    try:
        connection.start()
        old = sandbox.status()["pid"]
        assert len(sandbox.status()["leases"]) == 1
        monkeypatch.setenv("YOKE_TEST_REATTACH", "fresh")
        os.kill(old, signal.SIGKILL)
        eventually(lambda: sandbox.status().get("pid") not in (None, old), timeout=10)
        eventually(
            lambda: connection.session_request("GET", "/client")["data"], timeout=10
        )
        assert (
            connection.session_request("GET")["data"]["title"]
            == "Existing conversation"
        )
        assert connection.client_config()["env"]["YOKE_TEST_REATTACH"] == "fresh"
        assert connection.client_config()["cwd"] == str(sandbox.root)
        assert len(sandbox.status()["leases"]) == 1
    finally:
        unsubscribe()
        connection.close()
    assert list(stream)
