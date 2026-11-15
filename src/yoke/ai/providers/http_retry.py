"""HTTP retry policy shared by provider transports.

Retry connect/pool timeouts and other transient failures before response bytes
arrive. Never replay a generation on timeout after consuming stream bytes, even
if those bytes have not yet formed a complete SSE event. Callers must track the
response stream, rather than using parsed text as evidence of progress.
Backoff includes bounded jitter; Retry-After accepts seconds and HTTP dates.
"""

from __future__ import annotations

import math
import re
import secrets
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import cast

import httpx

from yoke.ai.providers.base import (
    ProviderCancelledError,
    ProviderError,
    sleep_with_cancel,
)


@dataclass(frozen=True, slots=True)
class RetryDecision:
    """A mapped failure, optional delay, and provider-owned retry transition.

    A missing delay fails immediately. Recovery resends without sleeping and
    can use a separately allowed recovery attempt, as ZAI message repair does.
    The transition runs only when the driver will actually resend.
    """

    error: Exception
    delay: float | None = None
    recover: bool = False
    before_retry: Callable[[], None] | None = None
    on_backoff_cancel: Callable[[], None] | None = None


def run_with_retry[T](
    attempt: Callable[[int], T],
    classify: Callable[[Exception, int], RetryDecision],
    *,
    max_retries: int,
    cancel_requested: Callable[[], bool],
    sleep: Callable[[float], None],
    max_recoveries: int = 0,
    check_cancel: bool = True,
) -> T:
    """Own retry counting and backoff without knowing any provider policy."""
    number = 0
    recoveries = 0
    while True:
        if check_cancel and cancel_requested():
            raise ProviderCancelledError()
        try:
            return attempt(number)
        except Exception as exc:
            decision = classify(exc, number)
            extra_recovery = decision.recover and recoveries < max_recoveries
            if decision.delay is None or (number >= max_retries and not extra_recovery):
                if decision.error is exc:
                    raise
                raise decision.error from exc
            if decision.before_retry is not None:
                decision.before_retry()
            if decision.recover:
                recoveries += 1
            else:
                try:
                    sleep_with_cancel(
                        decision.delay, cancel_requested=cancel_requested, sleep=sleep
                    )
                except ProviderCancelledError:
                    if decision.on_backoff_cancel is not None:
                        decision.on_backoff_cancel()
                    raise
        number += 1


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
    return parse_retry_after(response.headers.get("retry-after"))


def parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header value from any transport."""
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
    text = response.text.strip()
    if "<" in text and re.search(r"</?[a-zA-Z][^>]*>", text):
        return response.reason_phrase or f"HTTP {response.status_code}"
    return text[:500] or f"HTTP {response.status_code}"


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


def response_error(
    response: httpx.Response, *, prefix: str = "Provider"
) -> ProviderError | None:
    """Translate an unsuccessful HTTP response without deciding whether to retry."""
    from yoke.ai.providers.base import ProviderRateLimitError, ProviderServerError

    if not response.is_error:
        return None
    detail = error_detail(response)
    if response.status_code == 429:
        return ProviderRateLimitError(
            f"{prefix} request was rate limited: {detail}",
            retry_after_seconds=retry_after_seconds(response),
        )
    if retryable_status(response.status_code):
        return ProviderServerError(
            f"{prefix} server error: {detail}", status_code=response.status_code
        )
    return ProviderError(
        f"{prefix} request failed: {detail}", status_code=response.status_code
    )


@dataclass(frozen=True, slots=True)
class HTTPRetryPolicy:
    """Retry policy for chat-completions and stateless Responses clients."""

    provider_name: str
    model: str
    max_retries: int
    backoff: float
    maximum: float
    prefix: str = "Provider"
    read_timeout_fail_fast: bool = False
    check_timeout_cancel: bool = True
    timeout_as_request_error: bool = False
    reset_client: Callable[[], None] | None = None

    def classify(
        self, error: Exception, attempt: int, *, cancel_requested: Callable[[], bool]
    ) -> RetryDecision:
        from yoke.ai.providers.base import ProviderRateLimitError, ProviderServerError
        from yoke.ai.providers.openai_compat.events import emit_retry_event

        network_error = isinstance(error, httpx.RequestError)
        retryable = True
        if (
            isinstance(error, httpx.TimeoutException)
            and not self.timeout_as_request_error
        ):
            if self.check_timeout_cancel and cancel_requested():
                return RetryDecision(ProviderCancelledError())
            mapped = ProviderError(f"{self.prefix} request timed out.")
            retryable = not (
                self.read_timeout_fail_fast and isinstance(error, httpx.ReadTimeout)
            )
        elif isinstance(error, httpx.RequestError):
            if cancel_requested():
                return RetryDecision(ProviderCancelledError())
            mapped = ProviderError(f"{self.prefix} request failed: {error}")
            retryable = should_retry_request_error(error)
        elif isinstance(error, ProviderRateLimitError | ProviderServerError):
            mapped = error
        else:
            return RetryDecision(error)
        if not retryable:
            return RetryDecision(mapped)
        retry_after = (
            mapped.retry_after_seconds
            if isinstance(mapped, ProviderRateLimitError)
            else None
        )
        delay = backoff_seconds(attempt, self.backoff, self.maximum, retry_after)

        def before_retry() -> None:
            if network_error and self.reset_client is not None:
                self.reset_client()
            emit_retry_event(
                mapped,
                provider_name=self.provider_name,
                model_id=self.model,
                attempt=attempt,
                max_retries=self.max_retries,
                wait_seconds=delay,
            )

        return RetryDecision(mapped, delay, before_retry=before_retry)
