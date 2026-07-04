# ruff: noqa: D100,D103,S101

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest
from websockets.exceptions import ConnectionClosedError

from yoke.agent.models import Message
from yoke.ai.providers.base import ProviderCancelledError
from yoke.ai.providers.base import ProviderError
from yoke.ai.providers.codex.subscription import OAuthCredentials
from yoke.ai.providers.codex.subscription import convert_messages
from yoke.ai.providers.codex.websockets import CodexWebSockets
from yoke.ai.providers.codex.websockets import CodexWebSocketsConfig
from yoke.ai.providers.codex.websockets import CodexWebSocketConnection
from yoke.ai.providers.codex.websockets import CodexWebSocketParseState
from yoke.ai.providers.codex.websockets import CodexWebSocketTimeoutError
from yoke.ai.providers.codex.websockets import CodexPreviousResponseNotFoundError
from yoke.ai.providers.codex.websockets import RESPONSES_WEBSOCKETS_BETA
from yoke.ai.providers.codex.websockets import X_CODEX_TURN_STATE_HEADER
from yoke.ai.providers.codex.websockets import base_url_for_domain
from yoke.ai.providers.codex.websockets import build_message_from_websocket_state
from yoke.ai.providers.codex.websockets import handle_websocket_event
from yoke.ai.providers.codex.websockets import map_websocket_error_event
from yoke.ai.providers.codex.websockets import optional_float_env
from yoke.ai.providers.codex.websockets import register_provider
from yoke.ai.providers.codex.websockets import websocket_url_for_base


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


def test_base_url_for_domain_appends_backend_api() -> None:
    assert (
        base_url_for_domain("https://codexlb.dakixr.dev")
        == "https://codexlb.dakixr.dev/backend-api"
    )


def test_optional_float_env_parses_disabled_values() -> None:
    assert optional_float_env(None, default=None) is None
    assert optional_float_env("off", default=20.0) is None
    assert optional_float_env("0", default=20.0) is None
    assert optional_float_env("30", default=None) == 30.0


def test_codex_websockets_default_timeout_matches_codex_idle_timeout(
    tmp_path: Path,
) -> None:
    class Context:
        home = tmp_path
        env: dict[str, str] = {}
        model = None
        reasoning_effort = None

    provider = register_provider(Context())

    assert provider.config.timeout_seconds == 300.0


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
        provider_name="codex",
        model_id="gpt-5.4",
    )

    assert message.text_content() == "done"
    assert message.usage is not None
    assert message.usage.provider_name == "codex"
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
        provider_name="codex",
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
        provider_name="codex",
        model_id="gpt-5.4",
    )

    assert message.text_content() == "streamed"


def test_response_failed_stale_previous_response_anchor_is_recoverable() -> None:
    error = map_websocket_error_event(
        {
            "type": "response.failed",
            "response": {
                "status": "failed",
                "error": {
                    "message": "Upstream previous response anchor expired; retry without previous_response_id.",
                    "type": "server_error",
                    "code": "codex_previous_response_stale",
                },
            },
        }
    )

    assert isinstance(error, CodexPreviousResponseNotFoundError)


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


def test_websocket_response_timeout_resets_after_each_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = iter(
        [
            '{"type":"response.output_text.delta","delta":"ok"}',
            '{"type":"response.completed","response":{"usage":{}}}',
        ]
    )
    monotonic_values = iter([0.0, 0.0, 9.0, 11.0, 11.0])

    class FakeWebSocket:
        def recv(self, timeout: float | None = None) -> str:
            del timeout
            return next(events)

        def close(self) -> None:
            return None

    monkeypatch.setattr(
        "yoke.ai.providers.codex.websockets.time.monotonic",
        lambda: next(monotonic_values),
    )
    provider = CodexWebSockets(
        CodexWebSocketsConfig(
            auth_path=tmp_path / "auth.json",
            accounts_dir=tmp_path / "accounts",
            auths_path=tmp_path / "auths.json",
            selection_path=tmp_path / "selection.json",
            timeout_seconds=10.0,
        )
    )

    message = provider._consume_websocket_response(
        cast(CodexWebSocketConnection, FakeWebSocket())
    )

    assert message.text_content() == "ok"


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
        provider_name="codex",
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


def test_codex_websockets_retries_send_time_closed_socket(tmp_path: Path) -> None:
    sent_payloads: list[str] = []
    factory_calls = 0

    class StaleWebSocket:
        def send(self, payload: str) -> None:
            del payload
            raise ConnectionClosedError(None, None)

        def recv(self, timeout: float | None = None) -> str:
            raise AssertionError("recv should not be called after send failure")

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
        del url, kwargs
        factory_calls += 1
        if factory_calls == 1:
            return StaleWebSocket()
        return FreshWebSocket()

    provider = CodexWebSockets(
        CodexWebSocketsConfig(
            auth_path=tmp_path / "auth.json",
            accounts_dir=tmp_path / "accounts",
            auths_path=tmp_path / "auths.json",
            selection_path=tmp_path / "selection.json",
            base_url="ws://127.0.0.1:8765/v1",
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
    assert len(sent_payloads) == 1


def test_codex_websockets_reconnects_closed_cached_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory_calls = 0

    class ClosedWebSocket:
        closed = True

        def send(self, payload: str) -> None:
            raise AssertionError("closed cached socket should not be reused")

        def recv(self, timeout: float | None = None) -> str:
            raise AssertionError("closed cached socket should not be reused")

        def close(self) -> None:
            return None

    class FreshWebSocket:
        closed = False

        def __init__(self) -> None:
            self.events = iter(
                [
                    '{"type":"response.output_text.delta","delta":"ok"}',
                    '{"type":"response.completed","response":{"usage":{}}}',
                ]
            )

        def send(self, payload: str) -> None:
            return None

        def recv(self, timeout: float | None = None) -> str:
            del timeout
            return next(self.events)

        def close(self) -> None:
            return None

    def fake_factory(url: str, **kwargs: object) -> FreshWebSocket:
        nonlocal factory_calls
        del url, kwargs
        factory_calls += 1
        return FreshWebSocket()

    provider = CodexWebSockets(
        CodexWebSocketsConfig(
            auth_path=tmp_path / "auth.json",
            accounts_dir=tmp_path / "accounts",
            auths_path=tmp_path / "auths.json",
            selection_path=tmp_path / "selection.json",
            base_url="ws://127.0.0.1:8765/v1",
        ),
        websocket_factory=fake_factory,
    )
    provider._websocket = cast(CodexWebSocketConnection, ClosedWebSocket())
    provider._websocket_credentials = OAuthCredentials(
        access="expired-access-token",
        refresh="refresh-token",
        expires=0,
        account_id="acct_old",
    )
    monkeypatch.setattr(
        provider,
        "_fresh_credentials",
        lambda: OAuthCredentials(
            access="fresh-access-token",
            refresh="refresh-token",
            expires=4_102_444_800_000,
            account_id="acct_new",
        ),
    )

    message = provider.complete([Message.user("hello")], [])

    assert message.text_content() == "ok"
    assert factory_calls == 1
    assert provider._websocket_credentials is not None
    assert provider._websocket_credentials.access == "fresh-access-token"


def test_codex_websockets_reuses_stable_prompt_cache_key(tmp_path: Path) -> None:
    sent_payloads: list[str] = []

    class FakeWebSocket:
        def __init__(self) -> None:
            self.events = iter(
                [
                    '{"type":"response.completed","response":{"output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"one"}]}],"usage":{}}}',
                    '{"type":"response.completed","response":{"output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"two"}]}],"usage":{}}}',
                ]
            )

        def send(self, payload: str) -> None:
            sent_payloads.append(payload)

        def recv(self, timeout: float | None = None) -> str:
            del timeout
            return next(self.events)

        def close(self) -> None:
            return None

    provider = CodexWebSockets(
        CodexWebSocketsConfig(
            auth_path=tmp_path / "auth.json",
            accounts_dir=tmp_path / "accounts",
            auths_path=tmp_path / "auths.json",
            selection_path=tmp_path / "selection.json",
            base_url="ws://127.0.0.1:8765/v1",
        ),
        websocket_factory=lambda url, **kwargs: FakeWebSocket(),
    )
    provider._websocket_credentials = OAuthCredentials(
        access="access-token",
        refresh="refresh-token",
        expires=4_102_444_800_000,
        account_id="acct_123",
    )

    provider.complete([Message.user("one")], [])
    provider.complete([Message.user("two")], [])

    payloads = [json_loads(payload) for payload in sent_payloads]
    assert payloads[0]["prompt_cache_key"] == payloads[1]["prompt_cache_key"]


def test_codex_websockets_sends_incremental_input_with_previous_response_id(
    tmp_path: Path,
) -> None:
    sent_payloads: list[str] = []
    image_url = "data:image/png;base64," + ("a" * 64)

    class FakeWebSocket:
        def __init__(self) -> None:
            self.events = iter(
                [
                    '{"type":"response.created","response":{"id":"resp-1"}}',
                    '{"type":"response.completed","response":{"id":"resp-1","output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"saw it"}]}],"usage":{}}}',
                    '{"type":"response.created","response":{"id":"resp-2"}}',
                    '{"type":"response.completed","response":{"id":"resp-2","output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"next"}]}],"usage":{}}}',
                ]
            )

        def send(self, payload: str) -> None:
            sent_payloads.append(payload)

        def recv(self, timeout: float | None = None) -> str:
            del timeout
            return next(self.events)

        def close(self) -> None:
            return None

    provider = CodexWebSockets(
        CodexWebSocketsConfig(
            auth_path=tmp_path / "auth.json",
            accounts_dir=tmp_path / "accounts",
            auths_path=tmp_path / "auths.json",
            selection_path=tmp_path / "selection.json",
            base_url="ws://127.0.0.1:8765/v1",
        ),
        websocket_factory=lambda url, **kwargs: FakeWebSocket(),
    )
    provider._websocket_credentials = OAuthCredentials(
        access="access-token",
        refresh="refresh-token",
        expires=4_102_444_800_000,
        account_id="acct_123",
    )
    image_message = Message.user(
        [
            {"type": "text", "text": "inspect this image"},
            {"type": "image_url", "image_url": {"url": image_url}},
        ]
    )
    first_response = Message.assistant("saw it")

    provider.complete([image_message], [])
    provider.complete([image_message, first_response, Message.user("continue")], [])

    first_payload = json_loads(sent_payloads[0])
    second_payload = json_loads(sent_payloads[1])
    assert first_payload.get("previous_response_id") is None
    assert image_url in sent_payloads[0]
    assert second_payload["previous_response_id"] == "resp-1"
    assert image_url not in sent_payloads[1]
    assert second_payload["input"] == [
        {"role": "user", "content": [{"type": "input_text", "text": "continue"}]}
    ]


def test_codex_websockets_sends_full_input_after_account_switch(
    tmp_path: Path,
) -> None:
    provider = CodexWebSockets(
        CodexWebSocketsConfig(
            auth_path=tmp_path / "auth.json",
            accounts_dir=tmp_path / "accounts",
            auths_path=tmp_path / "auths.json",
            selection_path=tmp_path / "selection.json",
            base_url="ws://127.0.0.1:8765/v1",
        ),
    )
    image_message = Message.user(
        [
            {"type": "text", "text": "inspect this image"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,aaa"}},
        ]
    )
    first_payload = provider._request_payload([image_message], [])
    provider._last_request_payload = first_payload
    provider._last_response_id = "resp-1"
    _, provider._last_response_items = convert_messages([Message.assistant("saw it")])
    provider._last_response_account_id = "acct_123"
    provider._last_response_auth_profile = "account-a"
    provider._websocket_credentials = OAuthCredentials(
        access="access-token-2",
        refresh="refresh-token-2",
        expires=4_102_444_800_000,
        account_id="acct_456",
    )
    provider._websocket_auth_profile = "account-b"

    websocket_payload = provider._prepare_websocket_payload(
        provider._request_payload(
            [image_message, Message.assistant("saw it"), Message.user("continue")], []
        )
    )

    assert websocket_payload.get("previous_response_id") is None
    assert (
        websocket_payload["input"]
        == provider._request_payload(
            [image_message, Message.assistant("saw it"), Message.user("continue")], []
        )["input"]
    )


def test_codex_websockets_sends_full_input_after_selection_changes(
    tmp_path: Path,
) -> None:
    selection_path = tmp_path / "selection.json"
    selection_path.write_text(
        json.dumps({"selected_profile": "account-b", "selected_at": 4_102_444_800}),
        encoding="utf-8",
    )
    provider = CodexWebSockets(
        CodexWebSocketsConfig(
            auth_path=tmp_path / "auth.json",
            accounts_dir=tmp_path / "accounts",
            auths_path=tmp_path / "auths.json",
            selection_path=selection_path,
            base_url="ws://127.0.0.1:8765/v1",
        ),
    )
    (tmp_path / "accounts").mkdir()
    first_payload = provider._request_payload([Message.user("one")], [])
    provider._last_request_payload = first_payload
    provider._last_response_id = "resp-1"
    _, provider._last_response_items = convert_messages([Message.assistant("one")])
    provider._last_response_account_id = "acct_123"
    provider._last_response_auth_profile = "account-a"
    provider._websocket_credentials = OAuthCredentials(
        access="access-token",
        refresh="refresh-token",
        expires=4_102_444_800_000,
        account_id="acct_123",
    )
    provider._websocket_auth_profile = "account-a"

    websocket_payload = provider._prepare_websocket_payload(
        provider._request_payload(
            [Message.user("one"), Message.assistant("one"), Message.user("two")],
            [],
        )
    )

    assert websocket_payload.get("previous_response_id") is None
    assert (
        websocket_payload["input"]
        == provider._request_payload(
            [Message.user("one"), Message.assistant("one"), Message.user("two")], []
        )["input"]
    )


def test_codex_websockets_falls_back_to_full_input_without_prefix_match(
    tmp_path: Path,
) -> None:
    sent_payloads: list[str] = []

    class FakeWebSocket:
        def __init__(self) -> None:
            self.events = iter(
                [
                    '{"type":"response.completed","response":{"id":"resp-1","output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"one"}]}],"usage":{}}}',
                    '{"type":"response.completed","response":{"id":"resp-2","output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"two"}]}],"usage":{}}}',
                ]
            )

        def send(self, payload: str) -> None:
            sent_payloads.append(payload)

        def recv(self, timeout: float | None = None) -> str:
            del timeout
            return next(self.events)

        def close(self) -> None:
            return None

    provider = CodexWebSockets(
        CodexWebSocketsConfig(
            auth_path=tmp_path / "auth.json",
            accounts_dir=tmp_path / "accounts",
            auths_path=tmp_path / "auths.json",
            selection_path=tmp_path / "selection.json",
            base_url="ws://127.0.0.1:8765/v1",
        ),
        websocket_factory=lambda url, **kwargs: FakeWebSocket(),
    )
    provider._websocket_credentials = OAuthCredentials(
        access="access-token",
        refresh="refresh-token",
        expires=4_102_444_800_000,
        account_id="acct_123",
    )

    provider.complete([Message.user("one")], [])
    provider.complete([Message.user("replacement")], [])

    second_payload = json_loads(sent_payloads[1])
    assert second_payload.get("previous_response_id") is None
    assert second_payload["input"] == [
        {"role": "user", "content": [{"type": "input_text", "text": "replacement"}]}
    ]


def test_codex_websockets_retries_full_input_when_previous_response_missing(
    tmp_path: Path,
) -> None:
    sent_payloads: list[str] = []
    sockets: list[object] = []

    class ErrorWebSocket:
        def send(self, payload: str) -> None:
            sent_payloads.append(payload)

        def recv(self, timeout: float | None = None) -> str:
            del timeout
            return json.dumps(
                {
                    "type": "response.failed",
                    "response": {
                        "status": "failed",
                        "error": {
                            "type": "server_error",
                            "code": "codex_previous_response_stale",
                            "message": "Upstream previous response anchor expired; retry without previous_response_id.",
                        },
                    },
                }
            )

        def close(self) -> None:
            return None

    class SuccessWebSocket:
        def send(self, payload: str) -> None:
            sent_payloads.append(payload)

        def recv(self, timeout: float | None = None) -> str:
            del timeout
            return '{"type":"response.completed","response":{"id":"resp-2","output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"ok"}]}],"usage":{}}}'

        def close(self) -> None:
            return None

    def fake_factory(url: str, **kwargs: object):
        del url, kwargs
        socket = ErrorWebSocket() if not sockets else SuccessWebSocket()
        sockets.append(socket)
        return socket

    provider = CodexWebSockets(
        CodexWebSocketsConfig(
            auth_path=tmp_path / "auth.json",
            accounts_dir=tmp_path / "accounts",
            auths_path=tmp_path / "auths.json",
            selection_path=tmp_path / "selection.json",
            base_url="ws://127.0.0.1:8765/v1",
            max_retries=1,
        ),
        websocket_factory=fake_factory,
        sleep=lambda _seconds: None,
    )
    provider._websocket_credentials = OAuthCredentials(
        access="access-token",
        refresh="refresh-token",
        expires=4_102_444_800_000,
        account_id="acct_123",
    )
    first_payload = provider._request_payload([Message.user("one")], [])
    first_payload["type"] = "response.create"
    provider._last_request_payload = first_payload
    provider._last_response_id = "resp-1"
    _, provider._last_response_items = convert_messages([Message.assistant("one")])
    provider._last_response_account_id = "acct_123"

    message = provider.complete(
        [Message.user("one"), Message.assistant("one"), Message.user("two")], []
    )

    assert message.text_content() == "ok"
    first_sent = json_loads(sent_payloads[0])
    second_sent = json_loads(sent_payloads[1])
    assert first_sent["previous_response_id"] == "resp-1"
    assert second_sent.get("previous_response_id") is None
    assert (
        second_sent["input"]
        == provider._request_payload(
            [Message.user("one"), Message.assistant("one"), Message.user("two")], []
        )["input"]
    )


def test_codex_websockets_captures_and_replays_turn_state(tmp_path: Path) -> None:
    sent_payloads: list[str] = []
    factory_headers: list[dict[str, str]] = []

    class FirstWebSocket:
        def __init__(self) -> None:
            self.events = iter(
                [
                    '{"type":"response.metadata","headers":{"x-codex-turn-state":"turn-123"}}',
                    '{"type":"response.completed","response":{"output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"one"}]}],"usage":{}}}',
                ]
            )

        def send(self, payload: str) -> None:
            sent_payloads.append(payload)

        def recv(self, timeout: float | None = None) -> str:
            del timeout
            return next(self.events)

        def close(self) -> None:
            return None

    class SecondWebSocket:
        def __init__(self) -> None:
            self.events = iter(
                [
                    '{"type":"response.completed","response":{"output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"two"}]}],"usage":{}}}',
                ]
            )

        def send(self, payload: str) -> None:
            sent_payloads.append(payload)

        def recv(self, timeout: float | None = None) -> str:
            del timeout
            return next(self.events)

        def close(self) -> None:
            return None

    sockets: list[CodexWebSocketConnection] = [FirstWebSocket(), SecondWebSocket()]

    def fake_factory(url: str, **kwargs: object) -> CodexWebSocketConnection:
        del url
        factory_headers.append(cast(dict[str, str], kwargs["additional_headers"]))
        return sockets.pop(0)

    provider = CodexWebSockets(
        CodexWebSocketsConfig(
            auth_path=tmp_path / "auth.json",
            accounts_dir=tmp_path / "accounts",
            auths_path=tmp_path / "auths.json",
            selection_path=tmp_path / "selection.json",
            base_url="ws://127.0.0.1:8765/v1",
        ),
        websocket_factory=fake_factory,
    )
    provider._websocket_credentials = OAuthCredentials(
        access="access-token",
        refresh="refresh-token",
        expires=4_102_444_800_000,
        account_id="acct_123",
    )

    provider.complete([Message.user("one")], [])
    provider._close_websocket(clear_credentials=False)
    provider.complete([Message.user("two")], [])

    first_payload = json_loads(sent_payloads[0])
    second_payload = json_loads(sent_payloads[1])
    second_metadata = second_payload["client_metadata"]
    assert isinstance(second_metadata, dict)
    typed_second_metadata = cast(dict[str, object], second_metadata)
    assert X_CODEX_TURN_STATE_HEADER not in first_payload
    assert typed_second_metadata[X_CODEX_TURN_STATE_HEADER] == "turn-123"
    assert X_CODEX_TURN_STATE_HEADER not in factory_headers[0]
    assert factory_headers[1][X_CODEX_TURN_STATE_HEADER] == "turn-123"


def test_codex_websockets_retries_timed_out_socket(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory_calls = 0

    class FakeWebSocket:
        def send(self, payload: str) -> None:
            del payload

        def recv(self, timeout: float | None = None) -> str:
            del timeout
            return '{"type":"response.completed","response":{"output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"ok"}]}],"usage":{}}}'

        def close(self) -> None:
            return None

    def fake_factory(url: str, **kwargs: object) -> FakeWebSocket:
        nonlocal factory_calls
        del url, kwargs
        factory_calls += 1
        return FakeWebSocket()

    provider = CodexWebSockets(
        CodexWebSocketsConfig(
            auth_path=tmp_path / "auth.json",
            accounts_dir=tmp_path / "accounts",
            auths_path=tmp_path / "auths.json",
            selection_path=tmp_path / "selection.json",
            base_url="ws://127.0.0.1:8765/v1",
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
    original_consume = provider._consume_websocket_response
    consume_calls = 0

    def fake_consume(
        websocket: CodexWebSocketConnection,
        *,
        cancel_requested: Callable[[], bool] | None = None,
    ) -> Message:
        del cancel_requested
        nonlocal consume_calls
        consume_calls += 1
        if consume_calls == 1:
            raise CodexWebSocketTimeoutError(
                "Codex WebSocket timed out waiting for response."
            )
        return original_consume(websocket)

    monkeypatch.setattr(provider, "_consume_websocket_response", fake_consume)

    message = provider.complete([Message.user("hello")], [])

    assert message.text_content() == "ok"
    assert factory_calls == 2


def json_loads(payload: str) -> dict[str, object]:
    import json

    decoded = json.loads(payload)
    assert isinstance(decoded, dict)
    return decoded
