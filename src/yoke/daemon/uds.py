"""HTTPX transports for the daemon's Unix socket.

The socket carries plain HTTP, but HTTPX builds a TLS context for every
transport, loading the system CA bundle (about 12 ms) each time. Clients open a
transport per request, so they share one context instead. No TLS handshake ever
happens on these transports, so the shared context is never used to connect.
"""

from __future__ import annotations

import ssl
from functools import cache
from pathlib import Path
from typing import Any

import httpx


@cache
def _shared_context() -> ssl.SSLContext:
    return ssl.create_default_context()


def unix_transport(socket_path: Path | str, **kwargs: Any) -> httpx.HTTPTransport:
    """Return a synchronous transport bound to *socket_path*."""
    return httpx.HTTPTransport(uds=str(socket_path), verify=_shared_context(), **kwargs)


def async_unix_transport(
    socket_path: Path | str, **kwargs: Any
) -> httpx.AsyncHTTPTransport:
    """Return an asynchronous transport bound to *socket_path*."""
    return httpx.AsyncHTTPTransport(
        uds=str(socket_path), verify=_shared_context(), **kwargs
    )
