"""Uvicorn lifecycle helpers for the privileged local daemon."""

from __future__ import annotations

import ipaddress
import os
import secrets
import socket
from types import FrameType
from urllib.parse import urlencode
import webbrowser

import uvicorn

from yoke.http.app import HttpAppSettings
from yoke.http.app import create_app
from yoke.http.services.event_broker import GlobalEventBroker


class _YokeServer(uvicorn.Server):
    """Uvicorn server that releases semantic SSE streams on the first signal."""

    def __init__(self, config: uvicorn.Config, broker: GlobalEventBroker) -> None:
        super().__init__(config)
        self._broker = broker

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        self._broker.close()
        super().handle_exit(sig, frame)


def is_loopback_host(host: str) -> bool:
    """Return whether a configured bind host is loopback-only."""
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def run_server(
    *,
    host: str,
    port: int,
    auth_token: str | None,
    allow_remote: bool,
    open_browser: bool = False,
    verbose: bool = False,
) -> int:
    """Bind and run the Yoke HTTP daemon, including reliable port-zero reporting."""
    if not is_loopback_host(host) and not allow_remote:
        raise ValueError("Remote binding requires --allow-remote.")
    token = auth_token or os.getenv("YOKE_HTTP_TOKEN") or secrets.token_urlsafe(32)
    app = create_app(HttpAppSettings(auth_token=token))
    sock = socket.socket(
        socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM
    )
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(128)
    selected_port = int(sock.getsockname()[1])
    print(f"Yoke HTTP listening on http://{host}:{selected_port}")
    print(f"Yoke HTTP bearer token: {token}")
    if open_browser:
        url = _browser_launch_url(host, selected_port, token)
        print(f"Opening Yoke web UI at {url.split('#', 1)[0].split('?', 1)[0]}")
        webbrowser.open(url, new=2, autoraise=True)
    config = uvicorn.Config(
        app,
        log_level="info" if verbose else "warning",
        access_log=verbose,
        timeout_graceful_shutdown=3,
    )
    server = _YokeServer(config, app.state.event_broker)
    try:
        server.run(sockets=[sock])
    finally:
        sock.close()
    return 0


def _browser_launch_url(host: str, port: int, token: str) -> str:
    browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    if ":" in browser_host and not browser_host.startswith("["):
        browser_host = f"[{browser_host}]"
    return f"http://{browser_host}:{port}/#{urlencode({'token': token})}"
