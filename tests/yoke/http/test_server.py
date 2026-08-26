from __future__ import annotations

# ruff: noqa: D100,D103,S101

from pathlib import Path

import pytest

from yoke.http.server import is_loopback_host
from yoke.http.server import run_server


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
        def __init__(self, _config: object) -> None:
            pass

        def run(self, *, sockets: list[object]) -> None:
            sock = sockets[0]
            observed["socket"] = sock
            observed["port"] = sock.getsockname()[1]  # type: ignore[attr-defined]

    monkeypatch.setattr("yoke.http.server.uvicorn.Server", FakeServer)
    monkeypatch.setattr(
        "yoke.http.server.create_app",
        lambda _settings: object(),
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
