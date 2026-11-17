"""Executable provider retry traces captured at 6ad9e18 before refactoring.

Fixtures contain actual attempts, delays, telemetry and terminal exceptions.
Only request IDs, clock timings and usage counters are omitted from Codex logs.
"""

# ruff: noqa: S101
from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedError, InvalidStatus
from websockets.frames import Close
from websockets.http11 import Response

from yoke.agent.models import Message, ToolCall, ToolFunction
from yoke.ai.providers.base import provider_event_handler
from yoke.ai.providers.codex.subscription import (
    CodexSubscriptionProvider,
    OAuthCredentials,
)
from yoke.ai.providers.codex.websocket.config import CodexWebSocketTimeoutError

from tests.yoke.ai.providers.transport_support import _provider, _response

# Shared HTTP retry policy and its per-provider wrappers are covered by
# test_shared_transport; these traces keep one representative HTTP transport,
# the SSE transport and the websocket transport.
KINDS = ("openai", "codex-http", "codex-ws")
SCENARIOS = (
    "429",
    "429-zero",
    "429-90",
    "503",
    "connect-timeout",
    "read-timeout",
    "partial-timeout",
    "cancel-backoff",
)
CASES = [
    f"{kind}/{scenario}/{ending}"
    for kind in KINDS
    for scenario in SCENARIOS
    for ending in ("recover", "exhaust")
]
CASES += [
    "zai/illegal-messages/recover",
    "zai/illegal-messages/exhaust",
    "codex-ws/disconnect/recover",
    "codex-ws/disconnect/exhaust",
    "codex-ws/anchor-missing/recover",
    "codex-ws/transient-marker/exhaust",
    "codex-http/stream-server/exhaust",
    "codex-ws/stream-server/exhaust",
]
FIXTURE = Path(__file__).with_name("fixtures") / "retry_behavior.json"


class TimeoutStream(httpx.SyncByteStream):
    def __iter__(self) -> Iterator[bytes]:
        yield b"data: "
        raise httpx.ReadTimeout("idle timeout")


def capture_trace(
    case: str, root: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    kind, scenario, ending = case.split("/")
    attempts: list[int] = []
    sleeps: list[float] = []
    events: list[dict[str, Any]] = []
    payloads: list[object] = []
    cancelled = False
    monkeypatch.setattr("secrets.randbelow", lambda _n: 0)
    credentials = OAuthCredentials(
        access="test", refresh="test", expires=4102444800000, account_id="acct"
    )
    monkeypatch.setattr(
        CodexSubscriptionProvider, "_fresh_credentials", lambda _self: credentials
    )

    def log(_self: object, event: str, **fields: object) -> None:
        if event in {
            "request_retry",
            "request_cancelled",
            "account_rotation",
            "request_error",
        }:
            events.append(
                {
                    "event": event,
                    **{
                        key: value
                        for key, value in fields.items()
                        if key
                        in {
                            "attempt",
                            "wait_seconds",
                            "reason",
                            "status_code",
                            "retry_after_used",
                        }
                    },
                }
            )

    monkeypatch.setattr(CodexSubscriptionProvider, "_log_event", log)

    def sleep(seconds: float) -> None:
        nonlocal cancelled
        sleeps.append(seconds)
        if scenario == "cancel-backoff":
            cancelled = True

    def fails() -> bool:
        return ending == "exhaust" or len(attempts) == 1

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(len(attempts))
        payloads.append(json.loads(request.content))
        if not fails():
            return _response(kind)
        if scenario in {"429", "429-zero", "429-90", "503", "cancel-backoff"}:
            headers = (
                {"retry-after": scenario.removeprefix("429-")}
                if scenario.startswith("429-")
                else {}
            )
            return httpx.Response(
                503 if scenario == "503" else 429,
                json={"error": {"message": "retry"}},
                headers=headers,
            )
        if scenario == "connect-timeout":
            raise httpx.ConnectTimeout("connect timeout")
        if scenario == "read-timeout":
            raise httpx.ReadTimeout("read timeout")
        if scenario == "partial-timeout":
            return httpx.Response(200, stream=TimeoutStream())
        if scenario == "illegal-messages":
            return httpx.Response(
                400, json={"error": {"message": "messages parameter is illegal"}}
            )
        if scenario == "stream-server":
            return httpx.Response(
                200,
                content='data: {"type":"error","error":{"code":"server_error","message":"retry"}}\n\n',
            )
        raise AssertionError(scenario)

    class Socket:
        frames = 0

        def send(self, payload: str) -> None:
            payloads.append(json.loads(payload))

        def recv(self, timeout: float | None = None) -> str:
            self.frames += 1
            if not fails():
                return json.dumps(
                    {
                        "type": "response.completed",
                        "response": {"id": "response-ok", "output": []},
                    }
                )
            if scenario in {"partial-timeout", "disconnect"} and self.frames == 1:
                return '{"type":"response.output_text.delta","delta":"partial"}'
            if scenario == "disconnect":
                raise ConnectionClosedError(
                    Close(1011, "disconnected"), Close(1011, "disconnected"), True
                )
            if scenario in {"partial-timeout", "read-timeout"}:
                raise CodexWebSocketTimeoutError(
                    "Codex WebSocket timed out waiting for response."
                )
            code = {
                "anchor-missing": "previous_response_not_found",
                "transient-marker": "temporarily unavailable",
            }.get(scenario, "server_error")
            return json.dumps(
                {"type": "error", "error": {"code": code, "message": "retry"}}
            )

        def close(self) -> None:
            pass

    def websocket_factory(*_args: object, **_kwargs: object) -> Socket:
        attempts.append(len(attempts))
        if fails():
            if scenario in {"429", "429-zero", "429-90", "503", "cancel-backoff"}:
                headers = (
                    Headers({"retry-after": scenario.removeprefix("429-")})
                    if scenario.startswith("429-")
                    else Headers()
                )
                raise InvalidStatus(
                    Response(503 if scenario == "503" else 429, "retry", headers, b"")
                )
            if scenario == "connect-timeout":
                raise httpx.ConnectTimeout("connect timeout")
        return Socket()

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        provider = _provider(kind, root, client, sleep)
        provider.config.retry_backoff_seconds = 1
        provider.config.max_retry_backoff_seconds = 10
        if kind.startswith("go-"):
            provider._openai_provider.config.retry_backoff_seconds = 1
            provider._openai_provider.config.max_retry_backoff_seconds = 10
        if kind == "codex-ws":
            provider._websocket_factory = websocket_factory
        if kind == "zai":
            provider.config.debug_log_path = root / "zai.jsonl"
            if scenario == "illegal-messages":
                provider.config.max_retries = 0
        messages = [Message.user("hello")]
        if scenario == "illegal-messages":
            messages += [
                Message(
                    role="assistant",
                    content="",
                    tool_calls=[
                        ToolCall(
                            id="call",
                            function=ToolFunction(name="read", arguments="{}"),
                        )
                    ],
                ),
                Message.tool("call", "result"),
            ]
        result: dict[str, Any] = {}
        try:
            with provider_event_handler(
                lambda event, fields: events.append({"event": event, **fields})
            ):
                message = provider.complete_with_cancel(
                    messages, [], cancel_requested=lambda: cancelled
                )
            result["message"] = message.text_content()
        except Exception as exc:
            result["error"] = {"type": type(exc).__name__, "message": str(exc)}
        finally:
            provider.close()
        if kind == "zai" and (root / "zai.jsonl").exists():
            for line in (root / "zai.jsonl").read_text().splitlines():
                record = json.loads(line)
                events.append(
                    {key: value for key, value in record.items() if key != "timestamp"}
                )
    return {
        "attempts": attempts,
        "sleeps": sleeps,
        "events": events,
        "payloads": payloads,
        **result,
    }


@pytest.mark.parametrize("case", CASES)
def test_retry_trace(
    case: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = json.loads(FIXTURE.read_text())[case]
    actual = capture_trace(case, tmp_path, monkeypatch)
    assert actual == expected
