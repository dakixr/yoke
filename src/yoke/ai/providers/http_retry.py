"""HTTP retry policy shared by provider transports.

Retry connect/pool timeouts and other transient failures before response bytes
arrive. Never replay a generation on timeout after consuming stream bytes, even
if those bytes have not yet formed a complete SSE event. Callers must track the
response stream, rather than using parsed text as evidence of progress.
Backoff includes bounded jitter; Retry-After accepts seconds and HTTP dates.
"""

from __future__ import annotations

import secrets
import math
from collections.abc import Iterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import cast

import httpx

from yoke.ai.providers.base import ProviderError


def backoff_seconds(
    attempt: int,
    base: float,
    maximum: float,
    retry_after: float | None = None,
) -> float:
    """Return capped exponential backoff with up to ten percent jitter."""
    delay = max(0.0, base * (2**attempt) if retry_after is None else retry_after)
    jitter = secrets.randbelow(1000) / 1000 * min(1.0, delay * 0.1)
    return min(delay + jitter, maximum)


def retry_after_seconds(response: httpx.Response) -> float | None:
    """Parse Retry-After delta seconds or an absolute HTTP date."""
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        delay = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        delay = (retry_at - datetime.now(UTC)).total_seconds()
    return max(delay, 0.0) if math.isfinite(delay) else None


def error_detail(response: httpx.Response) -> str:
    """Read an error response and extract its nested or plain error message."""
    try:
        response.read()
    except (httpx.ResponseNotRead, httpx.CloseError):
        return f"HTTP {response.status_code}"
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        for key in ("error", "message", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                nested = value.get("message") or value.get("detail")
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
    return response.text.strip() or f"HTTP {response.status_code}"


def retryable_status(status: int) -> bool:
    """Classify rate limiting and server errors as retryable HTTP failures."""
    return status == 429 or 500 <= status < 600


def should_retry_request_error(
    error: httpx.RequestError, *, stream_started: bool = False
) -> bool:
    """Allow transient transport failures only before response bytes arrive."""
    return not stream_started and isinstance(
        error,
        httpx.TimeoutException
        | httpx.ConnectError
        | httpx.ReadError
        | httpx.RemoteProtocolError
        | httpx.WriteError,
    )


class StreamProgress(httpx.SyncByteStream):
    """Observe raw bytes without changing their boundaries or content."""

    def __init__(self, response: httpx.Response) -> None:
        self.started = response.is_stream_consumed and bool(response.content)
        self._stream = cast(httpx.SyncByteStream, response.stream)
        response.stream = self

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self._stream:
            if chunk:
                self.started = True
            yield chunk

    def close(self) -> None:
        """Forward stream closure to the underlying HTTP transport."""
        self._stream.close()


def post_response(
    client: httpx.Client, url: str, *, json: object, headers: dict[str, str]
) -> httpx.Response:
    """Read a POST response while refusing retries after partial response bytes."""
    with client.stream("POST", url, json=json, headers=headers) as response:
        progress = StreamProgress(response)
        try:
            response.read()
        except httpx.RequestError as exc:
            if progress.started:
                failure = (
                    "timed out" if isinstance(exc, httpx.TimeoutException) else "failed"
                )
                raise ProviderError(
                    f"Provider request {failure} after response bytes were received."
                ) from exc
            raise
        return response
