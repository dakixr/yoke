"""Proof of races, transport authorization, lifecycle and build skew."""

from __future__ import annotations


import httpx

from tests.yoke.daemon.support import DaemonSandbox, eventually
from yoke.daemon.client import unix_client
from yoke.daemon.instance import read_info


def create_session(sandbox: DaemonSandbox) -> None:
    with unix_client(sandbox.paths.socket) as http:
        response = http.post(
            "session",
            json={
                "id": "proof",
                "title": "Proof",
                "location": {"directory": str(sandbox.root)},
                "selection": {"provider": "fixture", "model": "echo"},
            },
        )
        assert response.status_code == 200, response.text


def slow_turn(sandbox: DaemonSandbox) -> None:
    create_session(sandbox)
    with unix_client(sandbox.paths.socket) as http:
        response = http.post("session/proof/prompt", json={"prompt": {"text": "slow"}})
        assert response.status_code == 200, response.text
    try:
        eventually(lambda: (sandbox.root / "started").exists())
    except AssertionError:
        raise AssertionError(sandbox.paths.log.read_text()) from None


def test_tcp_requires_token_while_socket_uses_peer_credentials(sandbox, monkeypatch):
    from yoke.daemon.paths import DaemonPaths

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(sandbox.root / ("x" * 100)))
    paths = DaemonPaths.default()
    paths.ensure()
    assert paths.socket == paths.state_dir / "run" / "daemon.sock"
    sandbox.paths = paths
    sandbox.start("--no-idle-exit")
    status = sandbox.status()
    info = read_info(sandbox.paths)
    assert info
    assert sandbox.paths.info.stat().st_mode & 0o777 == 0o600
    with httpx.Client(base_url=f"http://127.0.0.1:{status['port']}/api/v1/") as http:
        assert http.get("capabilities").status_code == 401
        assert http.post("daemon/restart", json={}).status_code == 401
        assert http.post("daemon/stop", json={}).status_code == 401
        assert (
            http.get(
                "capabilities", headers={"Authorization": f"Bearer {info.token}"}
            ).status_code
            == 200
        )
    with unix_client(sandbox.paths.socket) as http:
        assert http.get("health").json()["data"]["buildID"] == "test-a"
        assert http.get("capabilities").status_code == 200

    assert sandbox.connect().socket == paths.socket
    monkeypatch.delenv("XDG_RUNTIME_DIR")
    handle = sandbox.connect()
    assert handle.socket == sandbox.paths.state_dir / "run" / "daemon.sock"
    assert handle.socket.stat().st_mode & 0o777 == 0o600
    assert handle.socket.parent.stat().st_mode & 0o777 == 0o700
