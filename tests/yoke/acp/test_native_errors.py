"""Native request failures identify operations without disclosing payloads."""

from __future__ import annotations

import asyncio

import httpx
import pytest
from acp import RequestError

from yoke.acp.native import NativeClient


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (httpx.ConnectTimeout, "connection timeout"),
        (httpx.ReadTimeout, "read timeout"),
        (httpx.WriteTimeout, "write timeout"),
        (httpx.PoolTimeout, "connection pool timeout"),
        (httpx.ConnectError, "connection failure"),
        (httpx.RemoteProtocolError, "remote protocol failure"),
        (httpx.ReadError, "transport failure"),
        (None, "invalid JSON response"),
    ],
)
def test_failure_identifies_operation_without_leaking_or_retrying(
    failure: type[httpx.RequestError] | None, reason: str
) -> None:
    async def scenario() -> None:
        calls = 0

        def respond(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if failure is not None:
                raise failure("private exception detail", request=request)
            return httpx.Response(200, content=b"private response body")

        native = NativeClient("http://fixture", "private token")
        await native.http.aclose()
        native.http = httpx.AsyncClient(
            base_url="http://fixture/api/v1/", transport=httpx.MockTransport(respond)
        )
        try:
            with pytest.raises(RequestError) as caught:
                await native.data(
                    "POST",
                    "session/private-session/prompt?secret=private-query",
                    json={"text": "private prompt"},
                )
            message = str(caught.value)
            assert f"Native HTTP {reason} during POST session/prompt after " in message
            assert "private" not in message
            assert calls == 1
            assert caught.value.__suppress_context__ is True
        finally:
            await native.close()

    asyncio.run(scenario())


def test_cancellation_is_not_converted_to_native_error() -> None:
    async def scenario() -> None:
        async def respond(_request: httpx.Request) -> httpx.Response:
            raise asyncio.CancelledError

        native = NativeClient("http://fixture", "secret")
        await native.http.aclose()
        native.http = httpx.AsyncClient(
            base_url="http://fixture/", transport=httpx.MockTransport(respond)
        )
        try:
            with pytest.raises(asyncio.CancelledError):
                await native.request("GET", "session/id/history")
        finally:
            await native.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "path", ["https://private:secret@host/", "private", "session/id/private"]
)
def test_unknown_paths_are_not_echoed(path: str) -> None:
    from yoke.acp.observation.errors import native_request_failure

    error = native_request_failure(
        httpx.ReadError("private"), method="private", path=path, elapsed=1.25
    )
    assert "private" not in str(error)
    assert "after 1.2s" in str(error)
