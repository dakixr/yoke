"""A discovered daemon endpoint, including its TCP authentication capability."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from yoke.daemon.uds import async_unix_transport, unix_transport


@dataclass(frozen=True, slots=True)
class TcpEndpoint:
    """Only the loopback address is accepted; the token never appears in repr."""

    port: int
    token: str = field(repr=False)

    def __post_init__(self) -> None:
        if not 0 < self.port <= 65535 or not self.token:
            raise ValueError("Daemon TCP transport needs a port and bearer token")


type Endpoint = Path | TcpEndpoint


def client_options(endpoint: Endpoint, *, asynchronous: bool = False) -> dict[str, Any]:
    """Keep Unix HTTPX options unchanged and authenticate every TCP request."""
    if isinstance(endpoint, TcpEndpoint):
        return {
            "base_url": f"http://127.0.0.1:{endpoint.port}/api/v1/",
            "headers": {"Authorization": f"Bearer {endpoint.token}"},
        }
    transport = async_unix_transport if asynchronous else unix_transport
    return {"transport": transport(endpoint), "base_url": "http://yoke/api/v1/"}


def async_client(endpoint: Endpoint, **kwargs: Any) -> httpx.AsyncClient:
    """Open one authenticated async client for requests, events or leases."""
    return httpx.AsyncClient(
        **client_options(endpoint, asynchronous=True), trust_env=False, **kwargs
    )
