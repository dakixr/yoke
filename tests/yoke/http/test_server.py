from __future__ import annotations

# ruff: noqa: D100,D103,S101

from pathlib import Path
from types import SimpleNamespace
import asyncio
from urllib.parse import parse_qs
from urllib.parse import urlparse

import pytest

from yoke.http.server import is_loopback_host
from yoke.http.server import run_server
from yoke.http.services.event_broker import GlobalEventBroker
from yoke.http.app import HttpAppSettings
from yoke.http.app import create_app
from fastapi.testclient import TestClient


def test_loopback_detection_rejects_wildcard_and_remote_hosts() -> None:
    assert is_loopback_host("127.0.0.1") is True
    assert is_loopback_host("::1") is True
    assert is_loopback_host("localhost") is True
    assert is_loopback_host("0.0.0.0") is False
    assert is_loopback_host("::") is False
    assert is_loopback_host("example.com") is False


def test_run_server_rejects_remote_bind_without_explicit_opt_in() -> None:
    with pytest.raises(ValueError, match="--allow-remote"):
        run_server(
            host="0.0.0.0",
            port=0,
            auth_token="fixed-token",
            allow_remote=False,
        )


def test_run_server_reports_selected_port_and_closes_socket(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    observed: dict[str, object] = {}

    class FakeServer:
        def __init__(self, _config: object, _broker: object) -> None:
            pass

        def run(self, *, sockets: list[object]) -> None:
            sock = sockets[0]
            observed["socket"] = sock
            observed["port"] = sock.getsockname()[1]  # type: ignore[attr-defined]

    monkeypatch.setattr("yoke.http.server._YokeServer", FakeServer)
    monkeypatch.setattr(
        "yoke.http.server.create_app",
        lambda _settings: SimpleNamespace(state=SimpleNamespace(event_broker=object())),
    )
    monkeypatch.chdir(tmp_path)

    result = run_server(
        host="127.0.0.1",
        port=0,
        auth_token="fixed-token",
        allow_remote=False,
    )

    assert result == 0
    output = capsys.readouterr().out
    assert f"127.0.0.1:{observed['port']}" in output
    assert "fixed-token" in output
    assert observed["socket"].fileno() == -1  # type: ignore[attr-defined]


def test_run_server_is_quiet_by_default_and_verbose_enables_uvicorn_logs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    configs: list[object] = []

    class FakeServer:
        def __init__(self, config: object, _broker: object) -> None:
            configs.append(config)

        def run(self, *, sockets: list[object]) -> None:
            assert sockets

    monkeypatch.setattr("yoke.http.server._YokeServer", FakeServer)
    monkeypatch.setattr(
        "yoke.http.server.create_app",
        lambda _settings: SimpleNamespace(state=SimpleNamespace(event_broker=object())),
    )
    monkeypatch.chdir(tmp_path)

    run_server(
        host="127.0.0.1",
        port=0,
        auth_token="fixed-token",
        allow_remote=False,
    )
    quiet = configs.pop()
    assert getattr(quiet, "log_level") == "warning"
    assert getattr(quiet, "access_log") is False

    run_server(
        host="127.0.0.1",
        port=0,
        auth_token="fixed-token",
        allow_remote=False,
        verbose=True,
    )
    verbose = configs.pop()
    assert getattr(verbose, "log_level") == "info"
    assert getattr(verbose, "access_log") is True


def test_run_server_open_launches_browser_with_session_scoped_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    opened: list[str] = []

    class FakeServer:
        def __init__(self, _config: object, _broker: object) -> None:
            pass

        def run(self, *, sockets: list[object]) -> None:
            assert sockets

    monkeypatch.setattr("yoke.http.server._YokeServer", FakeServer)
    monkeypatch.setattr(
        "yoke.http.server.webbrowser.open",
        lambda url, **_kwargs: opened.append(url) or True,
    )
    monkeypatch.chdir(tmp_path)

    result = run_server(
        host="0.0.0.0",
        port=0,
        auth_token="fixed token/with?chars",
        allow_remote=True,
        open_browser=True,
    )

    assert result == 0
    assert len(opened) == 1
    parsed = urlparse(opened[0])
    assert parsed.hostname == "127.0.0.1"
    assert parsed.port is not None
    assert parsed.query == ""
    assert parse_qs(parsed.fragment) == {"token": ["fixed token/with?chars"]}


def test_event_broker_close_releases_live_subscribers() -> None:
    async def scenario() -> None:
        broker = GlobalEventBroker()
        subscription = broker.subscribe()
        broker.close()
        assert await asyncio.wait_for(subscription.queue.get(), timeout=0.2) is None

    asyncio.run(scenario())


def test_packaged_web_app_routes_and_assets_share_api_origin(tmp_path: Path) -> None:
    client = TestClient(
        create_app(
            HttpAppSettings(
                auth_token="fixed-token",
                session_directory=tmp_path / "sessions",
            )
        )
    )

    for path in ("/", "/new", "/session/example", "/settings"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["content-type"].startswith("text/html")
        assert 'id="app"' in response.text
        assert "/assets/js/main.js" in response.text

    asset = client.get("/assets/js/main.js")
    assert asset.status_code == 200
    assert asset.headers["cache-control"] == "no-store"
    assert asset.headers["content-type"].startswith("text/javascript")

    missing = client.get("/assets/not-real.js")
    assert missing.status_code == 404
    unknown = client.get("/not-an-app-route")
    assert unknown.status_code == 404

    denied = client.get("/api/v1/capabilities")
    assert denied.status_code == 401
    schema = client.get("/api/v1/openapi.json").json()
    assert "/" not in schema["paths"]
    assert "/session/{session_id}" not in schema["paths"]
