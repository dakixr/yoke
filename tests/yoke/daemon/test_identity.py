"""TCP discovery verifies the listener before sending its bearer capability."""

from __future__ import annotations

import hmac
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from yoke.daemon import client, instance
from yoke.daemon.lifecycle import DaemonLifecycle
from yoke.daemon.paths import DaemonPaths
from yoke.daemon.transport import TcpEndpoint
from yoke.http.app import HttpAppSettings, create_app


def proof(token, nonce, port):
    return hmac.digest(
        token.encode(), f"yoke-daemon-identity-v1\0{nonce}:{port}".encode(), "sha256"
    ).hex()


@pytest.mark.parametrize(
    "reply", ["valid", "wrong-token", "wrong-port", "replay", "empty", "not-json"]
)
def test_windows_verifies_identity_without_disclosing_token(
    tmp_path, monkeypatch, reply
):
    paths = DaemonPaths(tmp_path, tmp_path / "run")
    paths.ensure()
    instance.write_info(
        paths,
        instance.DaemonInfo(123, "3.35.0", "2", "", 12345, "private-token", True, 0),
    )
    monkeypatch.setattr(client, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(client, "daemon_alive", lambda paths: True)
    seen = []

    def get(http, route, **kwargs):
        request = http.build_request("GET", route, **kwargs)
        seen.append(request)
        if route == "daemon/identity":
            assert "authorization" not in request.headers
            assert "private-token" not in str(request.url)
            nonce = request.url.params["nonce"]
            data = (
                {
                    "proof": proof(
                        "wrong" if reply == "wrong-token" else "private-token",
                        "0" * 64 if reply == "replay" else nonce,
                        54321 if reply == "wrong-port" else 12345,
                    )
                }
                if reply != "empty"
                else {}
            )
            return (
                httpx.Response(200, text="not json")
                if reply == "not-json"
                else httpx.Response(200, json={"data": data})
            )
        return httpx.Response(200)

    monkeypatch.setattr(httpx.Client, "get", get)
    endpoint = client.find_daemon(paths)
    if reply == "valid":
        assert endpoint == TcpEndpoint(12345, "private-token")
        assert seen[-1].headers["authorization"] == "Bearer private-token"
    else:
        assert endpoint is None
        assert all("authorization" not in request.headers for request in seen)
    assert seen[0].url.path == "/api/v1/daemon/identity"


def test_identity_route_binds_nonce_and_actual_listener_port(tmp_path):
    app = create_app(
        HttpAppSettings(
            auth_token="private-token",
            session_directory=tmp_path / "sessions",
            daemon=DaemonLifecycle(idle_exit=False),
        )
    )
    with TestClient(app, base_url="http://127.0.0.1:12345") as http:
        nonce = "a" * 64
        response = http.get(
            "/api/v1/daemon/identity",
            params={"nonce": nonce},
            headers={"Host": "127.0.0.1:54321"},
        )
        assert response.status_code == 200
        assert response.json()["data"]["proof"] == proof("private-token", nonce, 12345)
        assert "private-token" not in response.text
        assert (
            http.get("/api/v1/daemon/identity", params={"nonce": "bad"}).status_code
            == 422
        )
