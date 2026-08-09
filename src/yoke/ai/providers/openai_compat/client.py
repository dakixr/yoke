"""Lazy synchronous HTTP client for OpenAI-compatible providers."""

from __future__ import annotations

from collections.abc import Callable
from threading import Lock

import httpx


class LazyHttpClient:
    """Create an owned HTTP client only when the first request starts."""

    def __init__(
        self,
        client: httpx.Client | None,
        factory: Callable[[], httpx.Client],
    ) -> None:
        self._client = client
        self._factory = factory
        self._lock = Lock()
        self._closed = False

    def post(
        self,
        url: str,
        *,
        json: object,
        headers: dict[str, str],
    ) -> httpx.Response:
        """Send a POST request through the initialized client."""
        return self.get_client().post(url, json=json, headers=headers)

    def close(self) -> None:
        """Close the client if a request initialized it."""
        with self._lock:
            self._closed = True
            client = self._client
            self._client = None
        if client is not None:
            client.close()

    def abort(self) -> None:
        """Close an active request while keeping the lazy client reusable."""
        with self._lock:
            client = self._client
            self._client = None
        if client is not None:
            client.close()

    def get_client(self) -> httpx.Client:
        """Return the client, creating it once when necessary."""
        client = self._client
        if client is not None:
            return client
        with self._lock:
            if self._closed:
                raise RuntimeError("HTTP client is closed")
            if self._client is None:
                self._client = self._factory()
            return self._client
