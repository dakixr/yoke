# ruff: noqa: D100,D103,S101

from __future__ import annotations

import base64
import json
from threading import Event
from pathlib import Path
from typing import cast

import httpx
import pytest

from yoke.agent.models import Message
from yoke.agent.models import ToolCall
from yoke.agent.models import ToolFunction
from yoke.ai.providers.codex.subscription import CodexSubscriptionConfig
from yoke.ai.providers.codex.subscription import CodexSubscriptionProvider
from yoke.ai.providers.codex.subscription import CodexProfileStore
from yoke.ai.providers.codex.subscription import CODEX_CLI_ORIGINATOR
from yoke.ai.providers.codex.subscription import OAUTH_PROVIDER_ID
from yoke.ai.providers.codex.subscription import OAuthCredentials
from yoke.ai.providers.codex.subscription import X_CODEX_TURN_STATE_HEADER
from yoke.ai.providers.codex.subscription import (
    X_OPENAI_INTERNAL_CODEX_RESPONSES_LITE_HEADER,
)
from yoke.ai.providers.codex.subscription import clamp_reasoning_effort
from yoke.ai.providers.codex.subscription import convert_messages
from yoke.ai.providers.codex.subscription import is_invalid_oauth_token_error
from yoke.ai.providers.codex.subscription import list_provider_models
from yoke.ai.providers.codex.subscription import register_provider
from yoke.ai.providers.base import ProviderCancelledError


def _write_fallback_auth(path: Path, credentials: OAuthCredentials) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({OAUTH_PROVIDER_ID: credentials.to_json()}),
        encoding="utf-8",
    )


def _fake_access_token(*, account_id: str, exp: int = 4_102_444_800) -> str:
    encoded_parts = []
    for payload in (
        {"alg": "none"},
        {
            "exp": exp,
            "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
        },
    ):
        raw = json.dumps(payload, separators=(",", ":")).encode()
        encoded_parts.append(base64.urlsafe_b64encode(raw).decode().rstrip("="))
    return f"{encoded_parts[0]}.{encoded_parts[1]}."


def test_invalid_oauth_token_error_detection() -> None:
    assert is_invalid_oauth_token_error(
        "Encountered invalidated oauth token for user, failing request"
    )
    assert is_invalid_oauth_token_error("OAuth token was revoked")
    assert not is_invalid_oauth_token_error("rate limited")


def test_codex_provider_default_timeout_matches_codex_idle_timeout(
    tmp_path: Path,
) -> None:
    class Context:
        home = tmp_path
        env: dict[str, str] = {}
        model = None
        reasoning_effort = None

    provider = register_provider(Context())

    assert provider.config.timeout_seconds == 300.0


def test_codex_catalog_includes_gpt_5_6_models() -> None:
    models = {model.id: model for model in list_provider_models(None)}

    assert {"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"} <= set(models)
    assert "gpt-5.6" not in models
    assert models["gpt-5.6-sol"].context_window_tokens == 400_000
    assert models["gpt-5.6-terra"].context_window_tokens == 400_000
    assert models["gpt-5.6-luna"].context_window_tokens == 400_000
    assert models["gpt-5.6-terra"].thinking_levels == (
        "none",
        "low",
        "medium",
        "high",
        "xhigh",
        "max",
    )
    assert models["gpt-5.6-luna"].supports_image_inputs is True


def test_codex_gpt_5_6_accepts_max_reasoning_effort(tmp_path: Path) -> None:
    provider = CodexSubscriptionProvider(
        CodexSubscriptionConfig(
            auth_path=tmp_path / "auth.json",
            accounts_dir=tmp_path / "accounts",
            auths_path=tmp_path / "auths.json",
            selection_path=tmp_path / "selection.json",
            model="gpt-5.6-terra",
            reasoning_effort="medium",
        )
    )

    try:
        provider.set_model("gpt-5.6-luna", reasoning_effort="max")

        assert provider.config.model == "gpt-5.6-luna"
        assert provider.config.reasoning_effort == "max"
        assert provider._request_payload([Message.user("hello")], [])["reasoning"] == {
            "effort": "max",
            "summary": "auto",
            "context": "all_turns",
        }
        assert (
            provider._request_payload([Message.user("hello")], [])[
                "parallel_tool_calls"
            ]
            is False
        )
        headers = provider._request_headers(
            OAuthCredentials(
                access="access-token",
                refresh="refresh-token",
                expires=4_102_444_800_000,
                account_id="acct_123",
            )
        )
        assert headers[X_OPENAI_INTERNAL_CODEX_RESPONSES_LITE_HEADER] == "true"
        assert headers["originator"] == CODEX_CLI_ORIGINATOR
    finally:
        provider.close()


def test_codex_reasoning_clamp_preserves_gpt_5_6_controls() -> None:
    assert clamp_reasoning_effort("gpt-5.6-sol", "none") == "none"
    assert clamp_reasoning_effort("gpt-5.6-terra", "max") == "max"
    assert clamp_reasoning_effort("gpt-5.5", "max") == "xhigh"


def test_convert_messages_drops_orphan_tool_outputs() -> None:
    _instructions, input_items = convert_messages(
        [
            Message.user("continue"),
            Message.tool("call_missing", '{"ok": true}'),
            Message.user("next"),
        ]
    )

    assert [item.get("type") for item in input_items] == [None, None]
    assert all(item.get("type") != "function_call_output" for item in input_items)


def test_convert_messages_drops_incomplete_tool_turn_outputs() -> None:
    _instructions, input_items = convert_messages(
        [
            Message.user("run tools"),
            Message(
                role="assistant",
                content="Running tools.",
                tool_calls=[
                    ToolCall(
                        id="call_done",
                        function=ToolFunction(
                            name="read",
                            arguments='{"path":"README.md"}',
                        ),
                    ),
                    ToolCall(
                        id="call_missing",
                        function=ToolFunction(
                            name="bash",
                            arguments='{"command":"sleep 600"}',
                        ),
                    ),
                ],
            ),
            Message.tool("call_done", '{"ok": true}'),
            Message.user("resume"),
        ]
    )

    assert [item.get("type") for item in input_items] == [None, None]
    assert all(
        item.get("type") not in {"function_call", "function_call_output"}
        for item in input_items
    )


def test_convert_messages_keeps_complete_tool_turn_outputs() -> None:
    _instructions, input_items = convert_messages(
        [
            Message.user("run tool"),
            Message(
                role="assistant",
                content="Running tool.",
                tool_calls=[
                    ToolCall(
                        id="call_read",
                        function=ToolFunction(
                            name="read",
                            arguments='{"path":"README.md"}',
                        ),
                    )
                ],
            ),
            Message.tool("call_read", '{"ok": true}'),
            Message.user("resume"),
        ]
    )

    assert [item.get("type") for item in input_items] == [
        None,
        None,
        "function_call",
        "function_call_output",
        None,
    ]


def test_codex_provider_relogs_via_fallback_auth_when_request_token_is_invalid(
    tmp_path: Path, monkeypatch
) -> None:
    auth_path = tmp_path / ".codex" / "auth.json"
    accounts_dir = tmp_path / ".codex-auth" / "accounts"
    selection_path = tmp_path / ".yoke" / "providers" / "codex-auth" / "selection.json"
    stale_credentials = OAuthCredentials(
        access="a.b.c",
        refresh="refresh-token",
        expires=4_102_444_800_000,
        account_id="acct_stale",
    )
    fresh_credentials = OAuthCredentials(
        access="x.y.z",
        refresh="refresh-token-2",
        expires=4_102_444_900_000,
        account_id="acct_fresh",
    )
    _write_fallback_auth(auth_path, stale_credentials)

    login_calls: list[str] = []

    def fake_login(originator: str) -> OAuthCredentials:
        login_calls.append(originator)
        return fresh_credentials

    monkeypatch.setattr(
        "yoke.ai.providers.codex.subscription.login_openai_codex",
        fake_login,
    )

    call_count = {"value": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        call_count["value"] += 1
        if call_count["value"] == 1:
            return httpx.Response(
                401,
                json={
                    "error": {
                        "message": "Encountered invalidated oauth token for user, failing request"
                    }
                },
            )
        return httpx.Response(
            200,
            text='event: response.completed\ndata: {"type":"response.completed","response":{"output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"done"}]}],"usage":{"input_tokens":1,"output_tokens":1,"total_tokens":2}}}\n\n',
            headers={"content-type": "text/event-stream"},
        )

    provider = CodexSubscriptionProvider(
        config=CodexSubscriptionConfig(
            auth_path=auth_path,
            accounts_dir=accounts_dir,
            auths_path=tmp_path / ".yoke" / "providers" / "codex-auth" / "auths.json",
            selection_path=selection_path,
            model="gpt-5.4",
            max_retries=1,
        ),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    try:
        message = provider.complete([Message.user("hello")], [])
    finally:
        provider.close()

    assert message.text_content() == "done"
    assert login_calls == ["yoke"]
    stored = json.loads(auth_path.read_text(encoding="utf-8"))
    assert stored[OAUTH_PROVIDER_ID]["accountId"] == "acct_fresh"


def test_codex_subscription_cancellation_closes_client_before_stream_enters(
    tmp_path: Path,
    monkeypatch,
) -> None:
    stream_entered = Event()
    client_closed = Event()
    credentials = OAuthCredentials(
        access="a.b.c",
        refresh="refresh-token",
        expires=4_102_444_800_000,
        account_id="acct",
    )

    class BlockingStream:
        def __enter__(self):
            stream_entered.set()
            assert client_closed.wait(timeout=2)
            raise httpx.RequestError("client closed")

        def __exit__(self, _exc_type, _exc, _tb) -> None:
            return None

    class BlockingClient:
        def stream(self, *_args, **_kwargs) -> BlockingStream:
            return BlockingStream()

        def close(self) -> None:
            client_closed.set()

    provider = CodexSubscriptionProvider(
        config=CodexSubscriptionConfig(
            auth_path=tmp_path / ".codex" / "auth.json",
            accounts_dir=tmp_path / ".codex-auth" / "accounts",
            auths_path=tmp_path / ".yoke" / "providers" / "codex-auth" / "auths.json",
            selection_path=tmp_path
            / ".yoke"
            / "providers"
            / "codex-auth"
            / "selection.json",
            model="gpt-5.4",
        )
    )
    monkeypatch.setattr(provider, "_client", cast(httpx.Client, BlockingClient()))
    monkeypatch.setattr(provider, "_fresh_credentials", lambda: credentials)

    with pytest.raises(ProviderCancelledError):
        provider.complete_with_cancel(
            [Message.user("hello")],
            [],
            cancel_requested=stream_entered.is_set,
        )

    assert client_closed.is_set()
    provider.close()


def test_codex_provider_reuses_stable_prompt_cache_key(tmp_path: Path) -> None:
    auth_path = tmp_path / ".codex" / "auth.json"
    credentials = OAuthCredentials(
        access="access-token",
        refresh="refresh-token",
        expires=4_102_444_800_000,
        account_id="acct_123",
    )
    _write_fallback_auth(auth_path, credentials)
    request_bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            text='event: response.completed\ndata: {"type":"response.completed","response":{"output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"done"}]}],"usage":{}}}\n\n',
            headers={"content-type": "text/event-stream"},
        )

    provider = CodexSubscriptionProvider(
        config=CodexSubscriptionConfig(
            auth_path=auth_path,
            accounts_dir=tmp_path / "accounts",
            auths_path=tmp_path / "auths.json",
            selection_path=tmp_path / "selection.json",
            model="gpt-5.4",
        ),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    try:
        provider.complete([Message.user("one")], [])
        provider.complete([Message.user("two")], [])
    finally:
        provider.close()

    assert (
        request_bodies[0]["prompt_cache_key"] == request_bodies[1]["prompt_cache_key"]
    )


def test_codex_provider_captures_and_replays_turn_state(tmp_path: Path) -> None:
    auth_path = tmp_path / ".codex" / "auth.json"
    credentials = OAuthCredentials(
        access="access-token",
        refresh="refresh-token",
        expires=4_102_444_800_000,
        account_id="acct_123",
    )
    _write_fallback_auth(auth_path, credentials)
    request_headers: list[httpx.Headers] = []
    request_bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_headers.append(request.headers)
        request_bodies.append(json.loads(request.content))
        body = (
            "event: response.metadata\n"
            'data: {"type":"response.metadata","headers":{"x-codex-turn-state":"turn-123"}}\n\n'
            "event: response.completed\n"
            'data: {"type":"response.completed","response":{"output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"done"}]}],"usage":{}}}\n\n'
            if len(request_bodies) == 1
            else "event: response.completed\n"
            'data: {"type":"response.completed","response":{"output":[{"type":"message","role":"assistant","content":[{"type":"output_text","text":"done"}]}],"usage":{}}}\n\n'
        )
        return httpx.Response(
            200,
            text=body,
            headers={"content-type": "text/event-stream"},
        )

    provider = CodexSubscriptionProvider(
        config=CodexSubscriptionConfig(
            auth_path=auth_path,
            accounts_dir=tmp_path / "accounts",
            auths_path=tmp_path / "auths.json",
            selection_path=tmp_path / "selection.json",
            model="gpt-5.4",
        ),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    try:
        provider.complete([Message.user("one")], [])
        provider.complete([Message.user("two")], [])
    finally:
        provider.close()

    assert X_CODEX_TURN_STATE_HEADER not in request_headers[0]
    assert request_headers[1][X_CODEX_TURN_STATE_HEADER] == "turn-123"
    assert "client_metadata" not in request_bodies[0]
    metadata = request_bodies[1]["client_metadata"]
    assert isinstance(metadata, dict)
    typed_metadata = cast(dict[str, object], metadata)
    assert typed_metadata[X_CODEX_TURN_STATE_HEADER] == "turn-123"


def test_codex_profile_store_keeps_local_profile_when_quota_probe_fails(
    tmp_path: Path, monkeypatch
) -> None:
    accounts_dir = tmp_path / ".codex-auth" / "accounts"
    profile_path = accounts_dir / "dr7878" / "auth.json"
    profile_path.parent.mkdir(parents=True)
    profile_path.write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": _fake_access_token(account_id="acct_dr7878"),
                    "refresh_token": "refresh-token",
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "yoke.ai.providers.codex.subscription.query_codex_quota",
        lambda payload: (_ for _ in ()).throw(RuntimeError("usage unavailable")),
    )

    store = CodexProfileStore(
        accounts_dir=accounts_dir,
        auths_path=tmp_path / ".yoke" / "providers" / "codex-auth" / "auths.json",
        selection_path=tmp_path
        / ".yoke"
        / "providers"
        / "codex-auth"
        / "selection.json",
        ttl_seconds=1800,
    )

    credentials, profile_name = store.fresh_credentials_with_profile()

    assert profile_name == "dr7878"
    assert credentials.account_id == "acct_dr7878"
