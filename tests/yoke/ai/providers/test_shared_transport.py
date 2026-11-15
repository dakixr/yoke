"""Cross-provider regressions for transport retries and isolated request state."""

# ruff: noqa: S101
from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest

from yoke.agent.models import Message
from yoke.ai.providers.base import (
    ProviderError,
)
from yoke.ai.providers.http_retry import (
    backoff_seconds,
    error_detail,
    retry_after_seconds,
    retryable_status,
    should_retry_request_error,
)

from tests.yoke.ai.providers.transport_support import (
    HTTP_KINDS,
    _provider,
    _response,
    _credentials as _credentials,
)


class _TimeoutStream(httpx.SyncByteStream):
    def __init__(self, prefix: bytes) -> None:
        self.prefix = prefix

    def __iter__(self) -> Iterator[bytes]:
        if self.prefix:
            yield self.prefix
        raise httpx.ReadTimeout("idle timeout")


@pytest.mark.parametrize("kind", HTTP_KINDS)
@pytest.mark.parametrize(
    "failure", ["connect", "pool", "headers", "empty-stream", "partial-line", "output"]
)
def test_timeout_replay_requires_no_response_bytes(
    kind: str, failure: str, tmp_path: Path
) -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts > 1:
            return _response(kind)
        if failure in ("connect", "pool", "headers"):
            raise {
                "connect": httpx.ConnectTimeout,
                "pool": httpx.PoolTimeout,
                "headers": httpx.ReadTimeout,
            }[failure]("timeout")
        prefix = b"" if failure == "empty-stream" else b"data: "
        if failure == "output":
            prefix = b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            if kind == "codex-http":
                prefix = (
                    b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n'
                )
        return httpx.Response(200, stream=_TimeoutStream(prefix))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = _provider(kind, tmp_path, client)
        try:
            if failure in ("partial-line", "output"):
                with pytest.raises(ProviderError, match="(interrupted|response bytes)"):
                    provider.complete([Message.user("hello")], [])
                assert attempts == 1
            else:
                assert (
                    provider.complete([Message.user("hello")], []).text_content()
                    == "ok"
                )
                assert attempts == 2
        finally:
            provider.close()


@pytest.mark.parametrize("kind", HTTP_KINDS)
@pytest.mark.parametrize("status", [429, 503])
def test_all_http_providers_retry_transient_status(
    kind: str, status: int, tmp_path: Path
) -> None:
    attempts = 0
    delays: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return (
            httpx.Response(
                status,
                json={"error": {"message": "retry"}},
                headers={"retry-after": "0"},
            )
            if attempts == 1
            else _response(kind)
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = _provider(kind, tmp_path, client, delays.append)
        try:
            assert provider.complete([Message.user("hello")], []).text_content() == "ok"
            assert attempts == 2
            assert delays == [0.0]
        finally:
            provider.close()


@pytest.mark.parametrize(
    "value,expected", [("0", 0.0), ("-2", 0.0), ("3.5", 3.5), ("invalid", None)]
)
def test_retry_after_delta(value: str, expected: float | None) -> None:
    assert (
        retry_after_seconds(httpx.Response(429, headers={"retry-after": value}))
        == expected
    )


def test_retry_after_http_date() -> None:
    value = format_datetime(datetime.now(UTC) + timedelta(seconds=120), usegmt=True)
    delay = retry_after_seconds(httpx.Response(429, headers={"retry-after": value}))
    assert delay is not None and 118 <= delay <= 120
    assert (
        retry_after_seconds(
            httpx.Response(
                429, headers={"retry-after": "Mon, 01 Jan 2001 00:00:00 GMT"}
            )
        )
        == 0
    )


def test_backoff_jitter_and_status_classification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("secrets.randbelow", lambda _n: 500)
    assert backoff_seconds(2, 1, 15) == 4.2
    assert backoff_seconds(9, 1, 15) == 15
    assert backoff_seconds(9, 1, 15, 0) == 0
    assert backoff_seconds(0, 1, 15, 2) == 2.1
    assert [
        code
        for code in (400, 401, 408, 429, 500, 503, 599, 600)
        if retryable_status(code)
    ] == [429, 500, 503, 599]
    assert should_retry_request_error(httpx.PoolTimeout("pool"))
    assert not should_retry_request_error(
        httpx.ReadTimeout("partial"), stream_started=True
    )


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({"error": {"message": " nested "}}, "nested"),
        ({"error": {"detail": "detail"}}, "detail"),
        ({"message": "plain"}, "plain"),
        ({"detail": "detail"}, "detail"),
    ],
)
def test_error_detail_variants(payload: dict[str, object], expected: str) -> None:
    assert error_detail(httpx.Response(400, json=payload)) == expected
    assert error_detail(httpx.Response(500, text=" broken ")) == "broken"
    assert error_detail(httpx.Response(503)) == "HTTP 503"
