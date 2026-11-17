"""Transport recovery exercises real HTTP/SSE over an isolated Unix socket."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack

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
            with ThreadPoolExecutor(max_workers=1) as pool, ExitStack() as cleanup:
                cleanup.callback(unsubscribe)
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


def test_connection_reattaches_and_resyncs_after_app_restart(tmp_path, monkeypatch):
    """A new app loses private attachments, but durable recovery survives."""
    from yoke.daemon.client import DaemonUnavailable, unix_client

    current = []

    def discover():
        if not current:
            raise DaemonUnavailable("Test daemon restarting")
        return current[0]

    def make_app():
        return create_app(
            HttpAppSettings(auth_token=None, session_directory=tmp_path / "db")
        )

    connection = None

    unsubscribe: Callable[[], None] | None = None

    try:
        first_app = make_app()
        with serve_app(first_app) as first:
            current[:] = [first]
            connection = SessionConnection("restart", discover=discover, cwd=tmp_path)
            connection.request(
                "POST",
                "session",
                json={"id": "restart", "location": {"directory": str(tmp_path)}},
            )
            stream, unsubscribe = connection.events.subscribe()
            iterator = iter(stream)
            connection.start()
            initial_generation = connection.generation
            initial = next(
                event for event in iterator if event["type"] == "client.resynced"
            )
            assert initial["payload"]["messages"] == []
            current.clear()
        monkeypatch.setenv("YOKE_TEST_RESTART", "fresh-attachment")
        second_app = make_app()
        with serve_app(second_app) as second:
            with unix_client(second.socket) as http:
                response = http.patch(
                    "session/restart", json={"title": "while offline"}
                )
                assert response.status_code == 200
            current[:] = [second]
            connection.wait_for_recovery(initial_generation, timeout=5)
            with ThreadPoolExecutor(max_workers=1) as pool:

                def recover():
                    seen = []
                    for event in iterator:
                        seen.append(event)
                        if event["type"] == "client.resynced":
                            return seen
                    raise AssertionError("Recovery stream ended")

                recovered = pool.submit(recover).result(timeout=5)
            updates = [e for e in recovered if e["type"] == "session.updated"]
            assert len(updates) == 1
            assert updates[0]["payload"]["title"] == "while offline"
            assert recovered[-1]["payload"]["seq"] > initial["payload"]["seq"]
            assert recovered[-1]["payload"]["messages"] == []
            attached = second_app.state.runtime_registry.get_if_loaded(
                "restart"
            ).resources.configuration.clients.select(connection.owner)
            assert (
                attached.environment.values["YOKE_TEST_RESTART"] == "fresh-attachment"
            )
            assert attached.environment.cwd == tmp_path
            connection.close()
            assert connection.session_request("GET", "/client")["data"] is None
    finally:
        if connection is not None:
            connection.close()
        if unsubscribe is not None:
            unsubscribe()
