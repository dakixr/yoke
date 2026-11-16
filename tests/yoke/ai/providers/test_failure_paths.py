"""Built-in provider behavior on rate limits, stalls, cut streams and cancellation.

Each test drives ``complete``/``complete_with_cancel`` on a real provider class
over an injected HTTP transport (or WebSocket factory for Codex WS) and asserts
the observable policy: attempts made, sleeps requested, the error raised.
"""

# ruff: noqa: D103, S101
from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedError, InvalidStatus
from websockets.frames import Close
from websockets.http11 import Response as WsResponse

from yoke.agent.models import Message
from yoke.ai.providers.base import ProviderCancelledError, ProviderError
from yoke.ai.providers.codex.subscription import (
    CodexSubscriptionConfig,
    CodexSubscriptionProvider,
)
from yoke.ai.providers.codex.websocket.config import CodexConfig
from yoke.ai.providers.codex.websocket.provider import CodexProvider
from yoke.ai.providers.openai_compat import (
    OpenAICompatibleConfig,
    OpenAICompatibleProvider,
)
from yoke.ai.providers.opencode_go import OpenCodeGoConfig, OpenCodeGoProvider
from yoke.ai.providers.zai import ZAIConfig, ZAIProvider

from tests.yoke.ai.providers.transport_support import (
    HTTP_KINDS,
    _credentials as _credentials,
    _response,
)

ALL_KINDS = (*HTTP_KINDS, "codex-ws")
HTML_502 = (
    "<!DOCTYPE html><html><head><title>502 Bad Gateway</title></head>"
    "<body><center><h1>502 Bad Gateway</h1></center><hr>nginx</body></html>"
)


def build(
    kind: str,
    tmp_path: Path,
    *,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] | None = None,
    websocket_factory: Callable[..., Any] | None = None,
    max_retries: int = 1,
    backoff: float = 0.0,
    max_backoff: float | None = None,
    timeout: float | None = None,
) -> Any:
    """Construct one provider kind through its public config and constructor."""
    retry: dict[str, Any] = {
        "max_retries": max_retries,
        "retry_backoff_seconds": backoff,
    }
    if max_backoff is not None:
        retry["max_retry_backoff_seconds"] = max_backoff
    common: dict[str, Any] = {"sleep": sleep or (lambda _seconds: None)}
    if kind == "openai":
        config = OpenAICompatibleConfig(api_key="test", model="test", **retry)
        if timeout is not None:
            config.timeout_seconds = timeout
        return OpenAICompatibleProvider(config, http_client=client, **common)
    if kind == "zai":
        config = ZAIConfig(api_key="test", **retry)
        if timeout is not None:
            config.read_idle_timeout_seconds = timeout
        return ZAIProvider(config, http_client=client, **common)
    if kind.startswith("go-"):
        model = (
            "deepseek-v4.1-flash" if kind == "go-chat" else "muse-spark-1.3-contributor"
        )
        config = OpenCodeGoConfig(api_key="test", model=model, **retry)
        if timeout is not None:
            config.timeout_seconds = timeout
        return OpenCodeGoProvider(config, http_client=client, **common)
    codex: dict[str, Any] = {
        "auth_path": tmp_path / "auth",
        "accounts_dir": tmp_path / "accounts",
        "auths_path": tmp_path / "auths",
        "selection_path": tmp_path / "selection",
        "logs_dir": tmp_path / "logs",
        "prompt_cache_key": "cache-fixed",
        **retry,
    }
    if timeout is not None:
        codex["timeout_seconds"] = timeout
    if kind == "codex-ws":
        return CodexProvider(
            CodexConfig(**codex), websocket_factory=websocket_factory, **common
        )
    return CodexSubscriptionProvider(
        CodexSubscriptionConfig(**codex), http_client=client, **common
    )


def default_cap(kind: str) -> float:
    config_type = {
        "openai": OpenAICompatibleConfig,
        "zai": ZAIConfig,
        "go-chat": OpenCodeGoConfig,
        "go-responses": OpenCodeGoConfig,
        "codex-http": CodexSubscriptionConfig,
        "codex-ws": CodexConfig,
    }[kind]
    return float(config_type.model_fields["max_retry_backoff_seconds"].default)


class FakeSocket:
    """A Codex WebSocket that plays a fixed list of frames or failures."""

    def __init__(self, frames: list[str | Exception], sent: list[dict]) -> None:
        self.frames = frames
        self.sent = sent

    def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    def recv(self, timeout: float | None = None) -> str:
        if not self.frames:
            # Nothing more to say: a stalled server, observed via recv timeouts.
            time.sleep(min(timeout or 0.01, 0.01))
            raise TimeoutError
        frame = self.frames.pop(0)
        if isinstance(frame, Exception):
            raise frame
        return frame

    def close(self) -> None:
        pass


COMPLETED_OK = json.dumps(
    {
        "type": "response.completed",
        "response": {
            "id": "resp-ok",
            "output": [
                {"type": "message", "content": [{"type": "output_text", "text": "ok"}]}
            ],
        },
    }
)
PARTIAL_DELTA = json.dumps({"type": "response.output_text.delta", "delta": "partial"})


def _closed() -> ConnectionClosedError:
    return ConnectionClosedError(Close(1011, "gone"), Close(1011, "gone"), True)


def _handshake_error(
    status: int, headers: dict[str, str], body: bytes = b""
) -> InvalidStatus:
    reason = {429: "Too Many Requests", 502: "Bad Gateway"}[status]
    return InvalidStatus(WsResponse(status, reason, Headers(headers), body))


def complete(
    provider: Any, cancel_requested: Callable[[], bool] | None = None
) -> Message:
    try:
        if cancel_requested is None:
            return provider.complete([Message.user("hello")], [])
        return provider.complete_with_cancel(
            [Message.user("hello")], [], cancel_requested=cancel_requested
        )
    finally:
        provider.close()


# --- 429 with Retry-After -------------------------------------------------

UNCAPPED_RETRY_AFTER = {"zai", "codex-http"}


@pytest.mark.parametrize("kind", HTTP_KINDS)
def test_retry_after_90_is_honored_up_to_the_backoff_cap(
    kind: str, tmp_path: Path
) -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                429,
                json={"error": {"message": "slow down"}},
                headers={"retry-after": "90"},
            )
        return _response(kind)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = build(kind, tmp_path, client=client, sleep=sleeps.append, backoff=1)
        assert complete(provider).text_content() == "ok"

    assert attempts == 2
    cap = default_cap(kind)
    if kind in UNCAPPED_RETRY_AFTER:
        # Z.AI and Codex HTTP wait exactly as long as the server asked, beyond
        # their own cap, as on main.
        assert cap < 90
        assert sleeps == [90.0]
    elif cap < 90:
        assert sleeps == [cap]
    else:
        # Within the cap the server's delay wins, plus at most 1s jitter.
        assert len(sleeps) == 1 and 90.0 <= sleeps[0] <= 91.0


@pytest.mark.parametrize("kind", ["openai", "zai", "codex-http"])
def test_explicit_backoff_cap_bounds_retry_after(kind: str, tmp_path: Path) -> None:
    sleeps: list[float] = []
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"retry-after": "90"})
        return _response(kind)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = build(
            kind,
            tmp_path,
            client=client,
            sleep=sleeps.append,
            backoff=1,
            max_backoff=30,
        )
        assert complete(provider).text_content() == "ok"
    assert sleeps == ([90.0] if kind in UNCAPPED_RETRY_AFTER else [30.0])


def test_codex_websocket_handshake_429_honors_retry_after(tmp_path: Path) -> None:
    sleeps: list[float] = []
    connections = 0

    def factory(*_args: object, **_kwargs: object) -> FakeSocket:
        nonlocal connections
        connections += 1
        if connections == 1:
            raise _handshake_error(429, {"retry-after": "90"})
        return FakeSocket([COMPLETED_OK], [])

    provider = build(
        "codex-ws", tmp_path, sleep=sleeps.append, websocket_factory=factory, backoff=1
    )
    assert complete(provider).text_content() == "ok"
    assert connections == 2
    # Codex WS shares Codex HTTP's policy: the server's full delay.
    assert sleeps == [90.0]


# --- server stalls before sending any bytes --------------------------------


class StalledServer:
    """Accept HTTP connections, read the request, and never answer."""

    def __init__(self) -> None:
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(16)
        self.listener.settimeout(0.02)
        self.stopped = threading.Event()
        self.port = int(self.listener.getsockname()[1])
        self.accepted = 0
        self._accepted_changed = threading.Condition()
        self.connections: list[socket.socket] = []
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        while not self.stopped.is_set():
            try:
                connection, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with self._accepted_changed:
                self.accepted += 1
                self.connections.append(connection)
                self._accepted_changed.notify_all()

    def wait_for_attempts(self, count: int) -> int:
        with self._accepted_changed:
            self._accepted_changed.wait_for(lambda: self.accepted >= count, timeout=2)
            return self.accepted

    def __enter__(self) -> StalledServer:
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stopped.set()
        self.thread.join(2)
        self.listener.close()
        for connection in self.connections:
            connection.close()


class ToLocal(httpx.BaseTransport):
    """Send every request to a local port, preserving method, path and body."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.inner = httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        request.url = request.url.copy_with(
            scheme="http", host="127.0.0.1", port=self.port
        )
        return self.inner.handle_request(request)

    def close(self) -> None:
        self.inner.close()


# Attempts a stall costs with max_retries=2: read timeouts on non-streaming
# chat completions fail fast because the server may still be generating.
STALL_ATTEMPTS = {
    "openai": 1,
    "go-chat": 1,
    "zai": 3,
    "go-responses": 3,
    "codex-http": 3,
}


@pytest.mark.parametrize("kind", HTTP_KINDS)
def test_stall_before_response_bytes_follows_read_timeout_policy(
    kind: str, tmp_path: Path
) -> None:
    sleeps: list[float] = []
    with (
        StalledServer() as server,
        httpx.Client(
            transport=ToLocal(server.port), timeout=httpx.Timeout(0.02)
        ) as client,
    ):
        provider = build(
            kind,
            tmp_path,
            client=client,
            sleep=sleeps.append,
            max_retries=2,
            timeout=0.02,
        )
        started = time.monotonic()
        with pytest.raises(ProviderError) as raised:
            complete(provider)
        elapsed = time.monotonic() - started
        accepted = server.wait_for_attempts(STALL_ATTEMPTS[kind])

    assert not isinstance(raised.value, ProviderCancelledError)
    assert accepted == STALL_ATTEMPTS[kind]
    assert len(sleeps) == STALL_ATTEMPTS[kind] - 1
    assert (
        "time" in str(raised.value).lower() or "interrupt" in str(raised.value).lower()
    )
    # Each attempt waits the injected read timeout, not the 900s default.
    assert elapsed < 0.25 * STALL_ATTEMPTS[kind] + 2.0


def test_codex_websocket_stall_before_first_frame_reconnects(tmp_path: Path) -> None:
    sleeps: list[float] = []
    sockets: list[FakeSocket] = []

    def factory(*_args: object, **_kwargs: object) -> FakeSocket:
        sockets.append(FakeSocket([], []))
        return sockets[-1]

    provider = build(
        "codex-ws",
        tmp_path,
        sleep=sleeps.append,
        websocket_factory=factory,
        max_retries=2,
        timeout=0.2,
    )
    with pytest.raises(ProviderError, match="timed out"):
        complete(provider)
    assert len(sockets) == 3
    assert len(sleeps) == 2


# --- the stream is cut after partial bytes ---------------------------------


class CutStream(httpx.SyncByteStream):
    def __init__(self, prefix: bytes, error: Exception) -> None:
        self.prefix = prefix
        self.error = error

    def __iter__(self) -> Iterator[bytes]:
        yield self.prefix
        raise self.error


def _partial(kind: str) -> bytes:
    if kind == "openai":
        return b'{"choices":[{"message":{"role":"assistant","content":"par'
    if kind == "go-responses":
        return (
            b'{"output":[{"type":"message","content":[{"type":"output_text","text":"par'
        )
    if kind == "codex-http":
        return b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n'
    return b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'


@pytest.mark.parametrize("kind", HTTP_KINDS)
@pytest.mark.parametrize(
    "cut",
    [
        httpx.RemoteProtocolError(
            "peer closed connection without sending complete message body"
        ),
        httpx.ReadError("connection reset by peer"),
    ],
    ids=["peer-closed", "reset"],
)
def test_http_stream_cut_after_partial_bytes_is_never_replayed(
    kind: str, cut: Exception, tmp_path: Path
) -> None:
    attempts = 0
    sleeps: list[float] = []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts > 1:
            return _response(kind)
        return httpx.Response(200, stream=CutStream(_partial(kind), cut))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = build(
            kind, tmp_path, client=client, sleep=sleeps.append, max_retries=3
        )
        with pytest.raises(ProviderError) as raised:
            complete(provider)

    assert not isinstance(raised.value, ProviderCancelledError)
    # The generation may have run server-side: one attempt, no backoff, no replay.
    assert attempts == 1
    assert sleeps == []


def test_codex_websocket_close_mid_response_reconnects_and_replays(
    tmp_path: Path,
) -> None:
    sent: list[dict] = []
    sockets: list[FakeSocket] = []

    def factory(*_args: object, **_kwargs: object) -> FakeSocket:
        frames: list[str | Exception] = (
            [PARTIAL_DELTA, _closed()] if not sockets else [COMPLETED_OK]
        )
        sockets.append(FakeSocket(frames, sent))
        return sockets[-1]

    sleeps: list[float] = []
    provider = build(
        "codex-ws", tmp_path, sleep=sleeps.append, websocket_factory=factory
    )
    message = complete(provider)

    assert len(sockets) == 2
    assert len(sent) == 2 and sent[0] == sent[1]
    # The partial text of the dropped generation never leaks into the result.
    assert message.text_content() == "ok"
    assert len(sleeps) == 1


# --- cancellation while sleeping in backoff --------------------------------


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_cancel_during_long_backoff_returns_promptly(kind: str, tmp_path: Path) -> None:
    failures: list[float] = []
    failed_once = threading.Event()
    cancelled = threading.Event()

    def fail() -> None:
        failures.append(time.monotonic())
        failed_once.set()

    def handler(_request: httpx.Request) -> httpx.Response:
        fail()
        return httpx.Response(429, headers={"retry-after": "60"})

    def factory(*_args: object, **_kwargs: object) -> FakeSocket:
        fail()
        raise _handshake_error(429, {"retry-after": "60"})

    def cancel_soon() -> None:
        assert failed_once.wait(10)
        # Let the provider enter its 60s backoff before cancelling.
        cancelled.wait(0.2)
        cancelled.set()

    canceller = threading.Thread(target=cancel_soon, daemon=True)
    canceller.start()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = build(
            kind,
            tmp_path,
            client=client,
            sleep=time.sleep,
            websocket_factory=factory,
            max_retries=3,
            backoff=60,
            max_backoff=60,
        )
        with pytest.raises(ProviderCancelledError):
            complete(provider, cancel_requested=cancelled.is_set)
        returned_at = time.monotonic()
    canceller.join(5)
    assert len(failures) == 1
    # Measured from the failed attempt: 0.2s until cancel, then prompt exit
    # from a backoff that would otherwise last 60s.
    assert returned_at - failures[0] < 1.0


# --- HTML error bodies -----------------------------------------------------


@pytest.mark.parametrize("kind", HTTP_KINDS)
def test_html_502_body_is_reported_by_reason_phrase(kind: str, tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text=HTML_502, headers={"content-type": "text/html"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = build(kind, tmp_path, client=client, max_retries=0)
        with pytest.raises(ProviderError) as raised:
            complete(provider)

    message = str(raised.value)
    assert "Bad Gateway" in message
    assert "<" not in message and "nginx" not in message


def test_codex_websocket_html_502_handshake_hides_body(tmp_path: Path) -> None:
    def factory(*_args: object, **_kwargs: object) -> FakeSocket:
        raise _handshake_error(502, {"content-type": "text/html"}, HTML_502.encode())

    provider = build("codex-ws", tmp_path, websocket_factory=factory, max_retries=0)
    with pytest.raises(ProviderError) as raised:
        complete(provider)
    message = str(raised.value)
    assert "502" in message
    assert "<" not in message and "nginx" not in message
