"""Regression coverage for client discovery, skew and spawn configuration."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from yoke.daemon import build, client, instance
from yoke.daemon.environment import spawn_environment
from yoke.daemon.lifecycle import DaemonLifecycle
from yoke.daemon.paths import DaemonPaths
from yoke.session.repository.utils import default_database_path


@pytest.mark.parametrize("versions", [("3.34.1", "3.35.0"), ("3.35.0", "3.35.0")])
def test_two_disk_builds_and_leases_cannot_ping_pong(tmp_path, monkeypatch, versions):
    paths = DaemonPaths(tmp_path, tmp_path / "run")
    builds = [f"{version}+g{tree}" for version, tree in zip(versions, ("aaaa", "bbbb"))]
    running = builds[0]
    restarts = []
    lifecycle = DaemonLifecycle(idle_exit=False)
    leases = [lifecycle.open_lease(f"client-{i}") for i in range(2)]
    monkeypatch.setattr(client, "find_daemon", lambda paths: paths.socket)
    monkeypatch.setattr(client, "protocol_version", lambda: "2")

    def status(request):
        return httpx.Response(
            200,
            json={
                "data": {
                    "pid": 123,
                    "buildID": running,
                    "protocolVersion": "2",
                    "busy": asyncio.run(lifecycle.busy()),
                    "databasePath": str(default_database_path().resolve()),
                    "leases": [lease.id for lease in lifecycle.leases()],
                }
            },
        )

    monkeypatch.setattr(
        client,
        "unix_client",
        lambda *a, **k: httpx.Client(
            base_url="http://yoke/", transport=httpx.MockTransport(status)
        ),
    )

    def restart(*args, **kwargs):
        nonlocal running
        restarts.append(running)
        running = client.fresh_build_id()
        for lease in leases:
            lifecycle.close_lease(lease)
        leases[:] = [lifecycle.open_lease(f"client-{i}") for i in range(2)]
        return True

    monkeypatch.setattr(client, "restart_daemon", restart)
    for _ in range(6):
        for disk in builds:
            monkeypatch.setattr(client, "build_id", lambda disk=disk: disk)
            monkeypatch.setattr(client, "fresh_build_id", lambda disk=disk: disk)
            client.connect_or_spawn(paths)
    assert running == builds[-1] if versions[0] != versions[1] else running == builds[0]
    assert len(restarts) == (1 if versions[0] != versions[1] else 0)
    assert len(lifecycle.leases()) == 2


@pytest.mark.parametrize("busy", [False, True])
def test_same_version_hash_skew_warns_and_protocol_skew_refuses(busy):
    for protocol, expected in (("2", "warn"), ("1", "refuse")):
        assert (
            client.skew_action(
                client_build="3.35.0+gaaaa",
                disk_build="3.35.0+gaaaa",
                daemon_build="3.35.0+gbbbb",
                client_protocol="2",
                daemon_protocol=protocol,
                busy=busy,
            )
            == expected
        )


def test_database_mismatch_refuses_before_restart(tmp_path, monkeypatch):
    paths = DaemonPaths(tmp_path, tmp_path / "run")
    mine = tmp_path / "client.db"
    theirs = tmp_path / "daemon.db"
    monkeypatch.setenv("YOKE_DB_PATH", str(mine))
    monkeypatch.setattr(client, "find_daemon", lambda paths: paths.socket)
    monkeypatch.setattr(client, "build_id", lambda: "3.35.0")
    monkeypatch.setattr(client, "fresh_build_id", lambda: "3.35.0")
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
                            "buildID": "3.34.1",
                            "protocolVersion": "2",
                            "busy": False,
                            "databasePath": str(theirs),
                        }
                    },
                )
            ),
        ),
    )
    restart = Mock()
    monkeypatch.setattr(client, "restart_daemon", restart)
    with pytest.raises(client.DaemonUnavailable) as error:
        client.connect_or_spawn(paths)
    assert str(mine) in str(error.value)
    assert str(theirs) in str(error.value)
    restart.assert_not_called()


@pytest.mark.parametrize(
    "name",
    [
        "HTTPS_PROXY",
        "HTTP_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "YOKE_DISABLE_TLS",
    ],
)
@pytest.mark.parametrize("lower", [False, True])
def test_spawn_preserves_proxy_and_tls_settings(name, lower):
    name = name.lower() if lower else name
    assert spawn_environment({name: "configured"})[name] == "configured"


def test_windows_stale_record_never_sends_a_request(tmp_path, monkeypatch):
    paths = DaemonPaths(tmp_path, tmp_path / "run")
    paths.ensure()
    instance.write_info(
        paths,
        instance.DaemonInfo(123, "3.35.0", "2", "", 12345, "private-token", True, 0),
    )
    monkeypatch.setattr(client, "sys", SimpleNamespace(platform="win32"))
    requests = []

    def request(*_args, **_kwargs):
        requests.append(True)
        return httpx.Response(200, json={"data": {"pid": 123}})

    monkeypatch.setattr(httpx.Client, "request", request)
    assert client.find_daemon(paths) is None
    assert requests == []


def test_fresh_build_id_has_short_ttl(monkeypatch):
    now = [100.0]
    monkeypatch.delenv(build.BUILD_ID_ENV, raising=False)
    monkeypatch.setattr(
        build, "time", SimpleNamespace(monotonic=lambda: now[0]), raising=False
    )
    disk = Mock(side_effect=["first", "second"])
    monkeypatch.setattr(build, "_disk_build_id", disk)
    assert build.fresh_build_id() == "first"
    assert build.fresh_build_id() == "first"
    now[0] += 2.1
    assert build.fresh_build_id() == "second"


def test_unrelated_enclosing_git_repository_is_not_a_yoke_build(tmp_path, monkeypatch):
    root = tmp_path / "someone-elses-project"
    source = root / ".venv" / "site-packages" / "yoke" / "daemon"
    source.mkdir(parents=True)
    (source.parent / "_version.py").write_text('__version__ = "3.35.0"\n')
    monkeypatch.setattr(build, "__file__", str(source / "build.py"))

    def git(cwd, *args):
        return str(root) if args == ("rev-parse", "--show-toplevel") else "abc123"

    monkeypatch.setattr(build, "_git", git)
    assert build._disk_build_id() == "3.35.0"


@pytest.mark.parametrize(
    "candidate,running,expected",
    [
        ("3.10.0+gnew", "3.9.9+gold", True),
        ("3.9.9+gold", "3.10.0+gnew", False),
        ("3.35.0+ga", "3.35.0+gb", False),
        ("unknown", "3.35.0", False),
        ("3.35.0", "unknown", False),
    ],
)
def test_release_order_does_not_use_hash_or_lexical_order(candidate, running, expected):
    assert build.newer_release(candidate, running) is expected


def test_spawn_resolves_database_relative_to_client_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = spawn_environment({"YOKE_DB_PATH": "data/client.db"})
    assert result["YOKE_DB_PATH"] == str(tmp_path / "data" / "client.db")


def test_windows_old_protocol_discovery_fails_with_upgrade_advice(
    tmp_path, monkeypatch
):
    paths = DaemonPaths(tmp_path, tmp_path / "run")
    paths.ensure()
    instance.write_info(
        paths,
        instance.DaemonInfo(123, "3.34.1", "1", "", 12345, "private-token", True, 0),
    )
    monkeypatch.setattr(client, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(client, "daemon_alive", lambda paths: True)
    monkeypatch.setattr(client, "protocol_version", lambda: "2")
    monkeypatch.setattr(client, "_verified_tcp", lambda endpoint: False)
    with pytest.raises(client.DaemonIncompatible, match="TCP identity.*Upgrade"):
        client.find_daemon(paths)
