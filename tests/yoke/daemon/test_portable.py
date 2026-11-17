"""Windows branches exercised without depending on the host platform."""

from __future__ import annotations

import errno
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from yoke.daemon import client, instance
from yoke.daemon.paths import DaemonPaths
from yoke.daemon.transport import TcpEndpoint, async_client, client_options


def test_discovery_selects_recorded_tcp_port_and_authenticates(tmp_path, monkeypatch):
    paths = DaemonPaths(tmp_path, tmp_path / "run")
    paths.ensure()
    info = instance.DaemonInfo(123, "build", "1", "", 12345, "private-token", True, 0)
    instance.write_info(paths, info)
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200)

    monkeypatch.setattr(client, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(
        httpx.Client,
        "get",
        lambda self, route: handle(self.build_request("GET", route)),
    )
    endpoint = client.find_daemon(paths)
    assert endpoint == TcpEndpoint(12345, "private-token")
    assert str(seen[0].url) == "http://127.0.0.1:12345/api/v1/capabilities"
    assert seen[0].headers["authorization"] == "Bearer private-token"
    assert "private-token" not in repr(endpoint)
    paths.info.unlink()
    assert client.find_daemon(paths) is None


def test_unix_options_keep_socket_transport_without_token(tmp_path):
    options = client_options(tmp_path / "daemon.sock")
    assert options["base_url"] == "http://yoke/api/v1/"
    assert isinstance(options["transport"], httpx.HTTPTransport)
    assert "headers" not in options


def test_async_tcp_requests_use_token():
    import asyncio

    async def scenario():
        async with async_client(TcpEndpoint(1234, "secret")) as http:
            request = http.build_request("GET", "event")
            assert str(request.url) == "http://127.0.0.1:1234/api/v1/event"
            assert request.headers["authorization"] == "Bearer secret"
            assert http._trust_env is False

    asyncio.run(scenario())


@pytest.mark.parametrize("port,token", [(0, "token"), (65536, "token"), (80, "")])
def test_tcp_endpoint_refuses_missing_capability(port, token):
    with pytest.raises(ValueError):
        TcpEndpoint(port, token)


def test_windows_spawn_flags_and_log_redirection(tmp_path, monkeypatch):
    paths = DaemonPaths(tmp_path, tmp_path / "run")
    executable = sys.executable
    monkeypatch.setattr(
        client, "sys", SimpleNamespace(platform="win32", executable=executable)
    )
    opened = Mock(wraps=client.open_spawn_log)
    monkeypatch.setattr(client, "open_spawn_log", opened)
    process = Mock()
    spawn = Mock(return_value=process)
    monkeypatch.setattr(client.subprocess, "Popen", spawn)
    assert client.spawn_daemon(paths) is process
    args, kwargs = spawn.call_args
    assert args[0] == [executable, "-P", "-m", "yoke", "serve", "--daemon"]
    assert kwargs["creationflags"] == 0x208
    assert "start_new_session" not in kwargs
    opened.assert_called_once_with(paths.log)
    assert kwargs["stdout"].closed
    assert kwargs["stderr"] == client.subprocess.STDOUT


def test_unix_spawn_keeps_new_session(monkeypatch):
    monkeypatch.setattr(client, "sys", SimpleNamespace(platform="linux"))
    assert client.spawn_options() == {"start_new_session": True}


def test_windows_lock_contends_probes_and_releases(tmp_path, monkeypatch):
    paths = DaemonPaths(tmp_path, tmp_path / "run")
    held = set()
    calls = []

    def locking(fd, mode, size):
        assert os.lseek(fd, 0, os.SEEK_CUR) == 0
        assert size == 1
        calls.append(fd)
        if mode == 0:
            assert fd in held
            held.remove(fd)
            return
        if held:
            raise OSError(errno.EACCES, "lock held")
        held.add(fd)

    monkeypatch.setitem(
        sys.modules, "msvcrt", SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=locking)
    )
    monkeypatch.setattr(instance, "sys", SimpleNamespace(platform="win32"))
    first = instance.InstanceLock.acquire(paths, wait_seconds=0)
    assert first is not None
    assert instance.daemon_alive(paths)
    assert instance.InstanceLock.acquire(paths, wait_seconds=0) is None
    first.release()
    with pytest.raises(OSError):
        os.fstat(calls[0])
    assert not held
    assert not instance.daemon_alive(paths)
    assert not held


def test_windows_paths_ignore_xdg_and_socket_length(tmp_path, monkeypatch):
    from yoke.daemon import paths as module

    monkeypatch.setattr(module, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "xdg"))
    paths = module.DaemonPaths.default()
    assert paths.run_dir == Path.home() / ".yoke" / "run"


def test_acp_daemon_uses_tcp_capability(monkeypatch):
    from yoke.acp import runner

    endpoint = TcpEndpoint(1234, "acp-token")
    monkeypatch.setattr(
        client, "connect_or_spawn", lambda: client.DaemonHandle(endpoint, 1, "a", "1")
    )
    native = Mock()
    monkeypatch.setattr(runner, "NativeClient", native)

    def serve(factory, *, lease_client):
        factory()
        assert lease_client == "acp"
        return 0

    monkeypatch.setattr(runner, "_serve", serve)
    assert runner.run_acp_with_daemon() == 0
    native.assert_called_once_with("http://127.0.0.1:1234", "acp-token")


def test_acp_updates_tcp_token_and_port_on_rediscovery(tmp_path):
    import asyncio

    from yoke.acp.native import NativeClient

    async def scenario():
        native = NativeClient("http://127.0.0.1:1234", "old")
        original = native.http
        try:
            native.use_daemon_endpoint(TcpEndpoint(4321, "new"))
            request = native.http.build_request("GET", "event")
            assert str(request.url) == "http://127.0.0.1:4321/api/v1/event"
            assert request.headers["Authorization"] == "Bearer new"
            assert native.http is original
            # Unix discovery must not mutate its existing socket HTTP client.
            native.use_daemon_endpoint(tmp_path / "daemon.sock")
            assert native.http.base_url == "http://127.0.0.1:4321/api/v1/"
        finally:
            await native.close()

    asyncio.run(scenario())


def test_wait_for_tcp_exit_discovers_a_replacement_holding_the_lock(
    tmp_path, monkeypatch
):
    old = TcpEndpoint(1234, "old-token")
    replacement = TcpEndpoint(4321, "new-token")
    seen = []
    monkeypatch.setattr(client, "find_daemon", lambda paths: replacement)
    monkeypatch.setattr(client, "daemon_alive", lambda paths: True)

    def pid(endpoint):
        seen.append(endpoint)
        return 222

    monkeypatch.setattr(client, "_daemon_pid", pid)
    client.wait_for_exit(
        old, 111, timeout=0.1, paths=DaemonPaths(tmp_path, tmp_path / "run")
    )
    assert seen == [replacement]


def test_spawn_environment_is_allowlisted(tmp_path, monkeypatch):
    paths = DaemonPaths(tmp_path, tmp_path / "run")
    for key, value in {
        "YOKE_ROOT_SESSION_ID": "wrong-owner",
        "YOKE_PARENT_RUN_ID": "wrong-run",
        "YOKE_AGENT_RUN_HOST": "wrong-host",
        "YOKE_HTTP_TOKEN": "ambient-token",
        "OPENAI_API_KEY": "ambient-secret",
        "YOKE_CODEX_API_KEY": "ambient-secret",
        "VIRTUAL_ENV": "/client/venv",
        "PATH": os.pathsep.join(["/client/venv/bin", "/usr/bin"]),
        "LANG": "en_US.UTF-8",
        "LC_CTYPE": "C.UTF-8",
        "SystemRoot": "C:\\Windows",
        "COMSPEC": "C:\\Windows\\cmd.exe",
        "YOKE_DB_PATH": str(tmp_path / "explicit.db"),
    }.items():
        monkeypatch.setenv(key, value)
    spawn = Mock(return_value=Mock())
    monkeypatch.setattr(client.subprocess, "Popen", spawn)
    client.spawn_daemon(paths)
    environment = {
        key.upper(): value for key, value in spawn.call_args.kwargs["env"].items()
    }
    for key in (
        "YOKE_ROOT_SESSION_ID",
        "YOKE_PARENT_RUN_ID",
        "YOKE_AGENT_RUN_HOST",
        "YOKE_HTTP_TOKEN",
        "OPENAI_API_KEY",
        "YOKE_CODEX_API_KEY",
        "VIRTUAL_ENV",
    ):
        assert key not in environment
    assert environment["PATH"] == "/usr/bin"
    for key in (
        "HOME",
        "USERPROFILE",
        "LANG",
        "LC_CTYPE",
        "SystemRoot",
        "COMSPEC",
        "YOKE_DB_PATH",
        "YOKE_SESSION_DIR",
        "YOKE_DAEMON_PORT",
    ):
        assert environment[key.upper()] == os.environ[key]
