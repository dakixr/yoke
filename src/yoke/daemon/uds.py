"""HTTPX transports for the daemon's Unix socket.

The socket carries plain HTTP, but HTTPX builds a TLS context for every
transport, loading the system CA bundle (about 12 ms) each time. Clients open a
transport per request, so they share one context instead. No TLS handshake ever
happens on these transports, so the shared context is never used to connect.
"""

from __future__ import annotations

import asyncio
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


def ignore_cancelled_socket_wakeups(loop: asyncio.AbstractEventLoop) -> None:
    """Drop anyio's harmless wakeup of an already-cancelled Unix-socket read.

    anyio waits for socket readiness with ``loop.add_reader(sock, f.set_result,
    None)`` and removes the reader in a done callback. When the waiting task is
    cancelled in the same loop iteration the socket becomes ready, the reader
    still fires and ``set_result`` raises ``InvalidStateError`` on the cancelled
    future. asyncio then prints that traceback, for example on a headless
    client's stderr as it detaches. Every other error reaches the default handler.
    """

    def handle(loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        callback = getattr(context.get("handle"), "_callback", None)
        target = getattr(callback, "__self__", None)
        if (
            isinstance(context.get("exception"), asyncio.InvalidStateError)
            and getattr(callback, "__name__", None) == "set_result"
            and isinstance(target, asyncio.Future)
            and target.cancelled()
        ):
            return
        loop.default_exception_handler(context)

    loop.set_exception_handler(handle)
