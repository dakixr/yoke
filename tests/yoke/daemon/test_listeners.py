"""Default loopback fallback and explicit port configuration use isolated sockets."""

from __future__ import annotations

import socket
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from yoke.daemon import server
from yoke.daemon.instance import read_info
from yoke.daemon.paths import DaemonPaths


@pytest.mark.parametrize("explicit", [False, True])
def test_default_port_falls_back_and_publishes_actual_port(
    tmp_path, monkeypatch, short_socket_dir, capsys, explicit
):
    paths = DaemonPaths(tmp_path, short_socket_dir)
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        monkeypatch.setattr(server, "DEFAULT_PORT", port)
        monkeypatch.setattr(server, "preload_runtime", lambda: None)
        monkeypatch.setenv("YOKE_HTTP_TOKEN", "service-token")
        observed = []

        def run(*, sockets):
            info = read_info(paths)
            assert info is not None
            assert info.port is not None and info.port != port
            assert info.token == "service-token"
            assert info.port in [
                s.getsockname()[1] for s in sockets if s.family == socket.AF_INET
            ]
            observed.append(info.port)

        build_server = Mock(return_value=Mock(run=run))
        monkeypatch.setattr(server, "_build_server", build_server)
        if explicit:
            with pytest.raises(OSError, match="unavailable"):
                server.run_daemon(paths=paths, port=port)
            build_server.assert_not_called()
        else:
            assert server.run_daemon(paths=paths) == 0
            assert observed
            assert "falling back" in capsys.readouterr().out
        assert read_info(paths) is None


@pytest.mark.parametrize(
    "argument,environment,expected",
    [(None, None, None), (8770, None, 8770), (None, "8770", 8770)],
)
def test_port_source_keeps_explicit_default_strict(
    monkeypatch, argument, environment, expected
):
    monkeypatch.delenv(server.PORT_ENV, raising=False)
    if environment is not None:
        monkeypatch.setenv(server.PORT_ENV, environment)
    run = Mock(return_value=0)
    monkeypatch.setattr(server, "run_daemon", run)
    server.run_daemon_from_environment(
        port=argument, idle_exit=False, verbose=False, session_directory=None
    )
    assert run.call_args.kwargs["port"] == expected


def test_status_reports_recorded_fallback_port(tmp_path, monkeypatch, short_socket_dir):
    import httpx
    from yoke.daemon import client
    from yoke.cli.daemon_app import daemon_app

    paths = DaemonPaths(tmp_path, short_socket_dir)
    monkeypatch.setattr(DaemonPaths, "default", lambda: paths)
    monkeypatch.setattr(client, "find_daemon", lambda paths: paths.socket)
    data = {
        "pid": 123,
        "buildID": "test",
        "protocolVersion": "1",
        "port": 54321,
        "socket": "test.sock",
        "idleExit": False,
        "busy": False,
        "restartPending": False,
        "leases": [],
    }
    monkeypatch.setattr(
        client,
        "unix_client",
        lambda endpoint: httpx.Client(
            base_url="http://yoke/",
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"data": data})
            ),
        ),
    )
    result = CliRunner().invoke(daemon_app, ["status"])
    assert result.exit_code == 0
    assert "http://127.0.0.1:54321" in result.output
