"""Uvicorn lifecycle helpers for the privileged local daemon."""

from __future__ import annotations

import ipaddress
import os
import secrets
import socket

import uvicorn

from yoke.http.app import HttpAppSettings
from yoke.http.app import create_app


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
) -> int:
    """Bind and run the Yoke HTTP daemon, including reliable port-zero reporting."""
    if not is_loopback_host(host) and not allow_remote:
        raise ValueError("Remote binding requires --allow-remote.")
    token = auth_token or os.getenv("YOKE_HTTP_TOKEN") or secrets.token_urlsafe(32)
    app = create_app(HttpAppSettings(auth_token=token))
    sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    sock.listen(128)
    selected_port = int(sock.getsockname()[1])
    print(f"Yoke HTTP listening on http://{host}:{selected_port}")
    print(f"Yoke HTTP bearer token: {token}")
    config = uvicorn.Config(app, log_level="info")
    server = uvicorn.Server(config)
    try:
        server.run(sockets=[sock])
    finally:
        sock.close()
    return 0
