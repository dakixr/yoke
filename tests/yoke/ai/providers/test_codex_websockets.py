# ruff: noqa: D100,D103,S101

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from websockets.exceptions import ConnectionClosedError

from yoke.agent.models import Message
from yoke.ai.providers.base import ProviderCancelledError
from yoke.ai.providers.base import ProviderError
from yoke.ai.providers.codex_subscription import OAuthCredentials
from yoke.ai.providers.codex_websockets import CodexWebSockets
from yoke.ai.providers.codex_websockets import CodexWebSocketsConfig
from yoke.ai.providers.codex_websockets import CodexWebSocketConnection
from yoke.ai.providers.codex_websockets import CodexWebSocketParseState
from yoke.ai.providers.codex_websockets import RESPONSES_WEBSOCKETS_BETA
from yoke.ai.providers.codex_websockets import build_message_from_websocket_state
from yoke.ai.providers.codex_websockets import handle_websocket_event
from yoke.ai.providers.codex_websockets import optional_float_env
from yoke.ai.providers.codex_websockets import websocket_url_for_base


def test_websocket_url_for_chatgpt_codex_base() -> None:
    assert (
        websocket_url_for_base("https://chatgpt.com/backend-api")
        == "wss://chatgpt.com/backend-api/codex/responses"
    )
    assert (
        websocket_url_for_base("https://chatgpt.com/backend-api/codex")
        == "wss://chatgpt.com/backend-api/codex/responses"
    )


def test_websocket_url_for_openai_compatible_v1_base() -> None:
    assert websocket_url_for_base("ws://127.0.0.1:8765/v1") == (
        "ws://127.0.0.1:8765/v1/responses"
    )


def test_optional_float_env_parses_disabled_values() -> None:
    assert optional_float_env(None, default=None) is None
    assert optional_float_env("off", default=20.0) is None
    assert optional_float_env("0", default=20.0) is None
    assert optional_float_env("30", default=None) == 30.0


def test_websocket_response_done_builds_message_from_output_item() -> None:
    state = CodexWebSocketParseState(text_parts=[], function_calls={})
    handle_websocket_event(
        {
            "type": "response.output_item.done",
            "item": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "done"}],
            },
        },
        state,
    )
    handle_websocket_event(
        {
            "type": "response.completed",
            "response": {
                "id": "resp-1",
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 2,
                    "total_tokens": 3,
                },
            },
        },
        state,
    )

    message = build_message_from_websocket_state(
        state,
        provider_name="codex-websockets",
        model_id="gpt-5.4",
    )

    assert message.text_content() == "done"
    assert message.usage is not None
    assert message.usage.provider_name == "codex-websockets"
    assert message.usage.total_tokens == 3


def test_websocket_response_prefers_deltas_over_output_item_snapshot() -> None:
    state = CodexWebSocketParseState(text_parts=[], function_calls={})
    handle_websocket_event(
        {"type": "response.output_text.delta", "delta": "streamed"},
        state,
    )
    handle_websocket_event(
        {
            "type": "response.output_item.done",
            "item": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "streamed"}],
            },
        },
        state,
    )
    handle_websocket_event(
        {"type": "response.done", "response": {"usage": {}}},
        state,
    )

    message = build_message_from_websocket_state(
        state,
        provider_name="codex-websockets",
        model_id="gpt-5.4",
    )

    assert message.text_content() == "streamed"


def test_websocket_response_prefers_deltas_over_completed_snapshot() -> None:
    state = CodexWebSocketParseState(text_parts=[], function_calls={})
    handle_websocket_event(
        {"type": "response.output_text.delta", "delta": "streamed"},
        state,
    )
    handle_websocket_event(
        {
            "type": "response.completed",
            "response": {
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "streamed"}],
                    }
                ],
                "usage": {},
            },
        },
        state,
    )

    message = build_message_from_websocket_state(
        state,
        provider_name="codex-websockets",
        model_id="gpt-5.4",
    )

    assert message.text_content() == "streamed"


def test_websocket_consume_uses_short_cancel_poll_timeout(tmp_path: Path) -> None:
    cancelled = False
    seen_timeouts: list[float | None] = []

    class FakeWebSocket:
        def recv(self, timeout: float | None = None) -> str:
            nonlocal cancelled
            seen_timeouts.append(timeout)
            cancelled = True
            return "{}"

        def close(self) -> None:
            return None

    provider = CodexWebSockets(
        CodexWebSocketsConfig(
            auth_path=tmp_path / "auth.json",
            accounts_dir=tmp_path / "accounts",
            auths_path=tmp_path / "auths.json",
            selection_path=tmp_path / "selection.json",
            timeout_seconds=60.0,
        )
    )

    with pytest.raises(ProviderCancelledError):
        provider._consume_websocket_response(
            cast(CodexWebSocketConnection, FakeWebSocket()),
            cancel_requested=lambda: cancelled,
        )

    assert seen_timeouts == [pytest.approx(0.1)]


def test_websocket_function_call_output_item_builds_tool_call() -> None:
    state = CodexWebSocketParseState(text_parts=[], function_calls={})
    handle_websocket_event(
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "call_id": "call-1",
                "name": "read",
                "arguments": '{"path":"README.md"}',
            },
        },
        state,
    )
    handle_websocket_event(
        {"type": "response.done", "response": {"usage": {}}},
        state,
    )

    message = build_message_from_websocket_state(
        state,
        provider_name="codex-websockets",
        model_id="gpt-5.4",
    )

    assert message.tool_calls is not None
    assert message.tool_calls[0].id == "call-1"
    assert message.tool_calls[0].function.name == "read"


def test_codex_websockets_complete_sends_request_frame_and_headers(
    tmp_path: Path,
) -> None:
    sent_payloads: list[str] = []
    factory_calls: list[dict[str, object]] = []

    class FakeWebSocket:
        def __init__(self) -> None:
            self.events = iter(
                [
                    '{"type":"response.output_item.done","item":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"ok"}]}}',
                    '{"type":"response.completed","response":{"id":"resp-1","usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2}}}',
                ]
            )

        def send(self, payload: str) -> None:
            sent_payloads.append(payload)

        def recv(self, timeout: float | None = None) -> str:
            del timeout
            return next(self.events)

        def close(self) -> None:
            return None

    def fake_factory(url: str, **kwargs: object) -> FakeWebSocket:
        factory_calls.append({"url": url, **kwargs})
        return FakeWebSocket()

    provider = CodexWebSockets(
        CodexWebSocketsConfig(
            auth_path=tmp_path / ".codex" / "auth.json",
            accounts_dir=tmp_path / ".codex-auth" / "accounts",
            auths_path=tmp_path / ".yoke" / "providers" / "codex-auth" / "auths.json",
            selection_path=tmp_path
            / ".yoke"
            / "providers"
            / "codex-auth"
            / "selection.json",
            base_url="ws://127.0.0.1:8765/v1",
            model="gpt-5.4",
            max_retries=0,
        ),
        websocket_factory=fake_factory,
    )
    provider._websocket_credentials = OAuthCredentials(
        access="access-token",
        refresh="refresh-token",
        expires=4_102_444_800_000,
        account_id="acct_123",
    )

    message = provider.complete([Message.user("hello")], [])

    assert message.text_content() == "ok"
    assert factory_calls[0]["url"] == "ws://127.0.0.1:8765/v1/responses"
    headers = cast(dict[str, str], factory_calls[0]["additional_headers"])
    assert isinstance(headers, dict)
    assert headers["Authorization"] == "Bearer access-token"
    assert headers["chatgpt-account-id"] == "acct_123"
    assert headers["OpenAI-Beta"] == RESPONSES_WEBSOCKETS_BETA
    assert factory_calls[0]["ping_interval"] is None
    assert factory_calls[0]["ping_timeout"] == 20.0
    assert '"type":"response.create"' in sent_payloads[0]
    assert '"model":"gpt-5.4"' in sent_payloads[0]


def test_codex_websockets_complete_preserves_non_oauth_provider_error(
    tmp_path: Path,
) -> None:
    class FakeWebSocket:
        def send(self, payload: str) -> None:
            del payload

        def recv(self, timeout: float | None = None) -> str:
            del timeout
            raise ProviderError("Codex WebSocket closed before response.completed.")

        def close(self) -> None:
            return None

    provider = CodexWebSockets(
        CodexWebSocketsConfig(
            auth_path=tmp_path / ".codex" / "auth.json",
            accounts_dir=tmp_path / ".codex-auth" / "accounts",
            auths_path=tmp_path / ".yoke" / "providers" / "codex-auth" / "auths.json",
            selection_path=tmp_path
            / ".yoke"
            / "providers"
            / "codex-auth"
            / "selection.json",
            base_url="ws://127.0.0.1:8765/v1",
            model="gpt-5.4",
            max_retries=0,
        ),
        websocket_factory=lambda url, **kwargs: FakeWebSocket(),
    )
    provider._websocket_credentials = OAuthCredentials(
        access="access-token",
        refresh="refresh-token",
        expires=4_102_444_800_000,
        account_id="acct_123",
    )

    with pytest.raises(ProviderError, match="closed before response.completed"):
        provider.complete([Message.user("hello")], [])


def test_codex_websockets_retries_stale_cached_socket(tmp_path: Path) -> None:
    sent_payloads: list[str] = []
    factory_headers: list[dict[str, str]] = []
    factory_calls = 0

    class StaleWebSocket:
        def send(self, payload: str) -> None:
            sent_payloads.append(payload)

        def recv(self, timeout: float | None = None) -> str:
            del timeout
            raise ConnectionClosedError(None, None)

        def close(self) -> None:
            return None

    class FreshWebSocket:
        def __init__(self) -> None:
            self.events = iter(
                [
                    '{"type":"response.output_text.delta","delta":"ok"}',
                    '{"type":"response.completed","response":{"usage":{}}}',
                ]
            )

        def send(self, payload: str) -> None:
            sent_payloads.append(payload)

        def recv(self, timeout: float | None = None) -> str:
            del timeout
            return next(self.events)

        def close(self) -> None:
            return None

    def fake_factory(url: str, **kwargs: object) -> CodexWebSocketConnection:
        nonlocal factory_calls
        del url
        factory_calls += 1
        headers = cast(dict[str, str], kwargs.get("additional_headers"))
        factory_headers.append(headers)
        if factory_calls == 1:
            return StaleWebSocket()
        return FreshWebSocket()

    provider = CodexWebSockets(
        CodexWebSocketsConfig(
            auth_path=tmp_path / ".codex" / "auth.json",
            accounts_dir=tmp_path / ".codex-auth" / "accounts",
            auths_path=tmp_path / ".yoke" / "providers" / "codex-auth" / "auths.json",
            selection_path=tmp_path
            / ".yoke"
            / "providers"
            / "codex-auth"
            / "selection.json",
            base_url="ws://127.0.0.1:8765/v1",
            model="gpt-5.4",
            max_retries=1,
        ),
        websocket_factory=fake_factory,
        sleep=lambda seconds: None,
    )
    provider._websocket_credentials = OAuthCredentials(
        access="access-token",
        refresh="refresh-token",
        expires=4_102_444_800_000,
        account_id="acct_123",
    )

    message = provider.complete([Message.user("hello")], [])

    assert message.text_content() == "ok"
    assert factory_calls == 2
    assert [headers["Authorization"] for headers in factory_headers] == [
        "Bearer access-token",
        "Bearer access-token",
    ]
    assert len(sent_payloads) == 2
