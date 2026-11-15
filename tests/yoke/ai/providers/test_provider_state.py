"""Provider identity, fork ownership, cancellation and typed-host regressions."""

# ruff: noqa: S101
from __future__ import annotations

import logging
from pathlib import Path
from threading import Event
from typing import Any, cast

import httpx
import pytest

from yoke.agent.models import Message
from yoke.ai.providers.base import (
    Provider,
    ProviderCancelledError,
    ProviderError,
    fork_provider,
    provider_identity,
)
from yoke.ai.providers.cancellation import cancellation_watcher
from yoke.ai.providers.codex.subscription import (
    CodexSubscriptionProvider,
)
from yoke.ai.providers.codex.subscription.host import CodexHTTPHost
from yoke.ai.providers.codex.websocket.host import CodexWebSocketHost
from yoke.ai.providers.codex.websocket.provider import CodexProvider

from tests.yoke.ai.providers.transport_support import (
    HTTP_KINDS,
    _provider,
    _response,
    _credentials as _credentials,
)


@pytest.mark.parametrize("kind", (*HTTP_KINDS, "codex-ws"))
def test_builtin_identity_catalog_and_fork(kind: str, tmp_path: Path) -> None:
    with httpx.Client(
        transport=httpx.MockTransport(lambda _r: _response(kind))
    ) as client:
        provider = _provider(kind, tmp_path, client)
        forked = cast(Any, fork_provider(provider))
        try:
            identity = provider_identity(provider)
            expected_name = {
                "openai": "openai-compatible",
                "zai": "zai",
                "go-chat": "opencode-go",
                "go-responses": "opencode-go",
                "codex-http": "codex",
                "codex-ws": "codex",
            }[kind]
            assert identity.name == expected_name
            assert (
                identity.model == provider.current_model_id() == provider.config.model
            )
            assert identity.reasoning_effort == provider.config.reasoning_effort
            assert forked is not provider
            assert forked.config is not provider.config
            assert provider_identity(forked) == identity
            selected = provider.current_model_info()
            assert selected is not None and selected.id == identity.model
            catalog = provider.list_models()
            catalog[0].display_name = "mutated"
            assert provider.list_models()[0].display_name != "mutated"
            if kind != "codex-ws":
                active = (
                    provider._client.get_client()
                    if kind == "openai"
                    else provider._client
                )
                borrowed = (
                    forked._client.get_client() if kind == "openai" else forked._client
                )
                assert active is borrowed is client
                assert not forked._owns_client
            if kind.startswith("codex-"):
                assert forked._prompt_cache_key == provider._prompt_cache_key
        finally:
            forked.close()
            assert not client.is_closed
            provider.close()


def test_codex_hosts_are_structurally_checked(tmp_path: Path) -> None:
    http = _provider("codex-http", tmp_path, None)
    websocket = _provider("codex-ws", tmp_path, None)
    # Concrete declarations make pyright validate both implementations.
    http_host: CodexHTTPHost = cast(CodexSubscriptionProvider, http)
    ws_host: CodexWebSocketHost = cast(CodexProvider, websocket)
    assert http_host.config.model == ws_host.config.model
    http.close()
    websocket.close()


def test_unknown_fork_logs_shared_state_and_false_isolation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Custom:
        def complete(
            self, messages: list[Message], tools: list[dict[str, object]]
        ) -> Message:
            return Message.assistant("ok")

    custom = Custom()
    with caplog.at_level(logging.WARNING):
        assert fork_provider(custom) is custom
    assert "request state is shared" in caplog.text

    class FalseFork(Custom):
        def fork_for_turn(self) -> Provider:
            return self

    false_fork = FalseFork()
    with caplog.at_level(logging.WARNING):
        assert fork_provider(false_fork) is false_fork
    assert "returned the original instance; request state is shared" in caplog.text


@pytest.mark.parametrize("repeat", [False, True])
def test_cancellation_watcher_aborts_before_rebuild(repeat: bool) -> None:
    aborted = Event()
    order: list[str] = []

    def abort() -> None:
        order.append("abort")
        aborted.set()

    with (
        pytest.raises(ProviderCancelledError),
        cancellation_watcher(
            lambda: True, abort, rebuild=lambda: order.append("rebuild"), repeat=repeat
        ),
    ):
        assert aborted.wait(2)
    assert order[0] == "abort"
    assert order[-1] == "rebuild"


def test_cancellation_watcher_normal_exit_and_error_cleanup() -> None:
    with cancellation_watcher(lambda: False, lambda: pytest.fail("abort")):
        pass
    with (
        pytest.raises(ValueError, match="original"),
        cancellation_watcher(None, lambda: pytest.fail("abort")),
    ):
        raise ValueError("original")


@pytest.mark.parametrize("kind", (*HTTP_KINDS, "codex-ws"))
def test_owned_forks_have_independent_transports(kind: str, tmp_path: Path) -> None:
    provider = _provider(kind, tmp_path, None)
    forked = cast(Any, fork_provider(provider))
    try:
        assert forked._client is not provider._client
        assert forked._owns_client and provider._owns_client
        if kind == "codex-ws":
            assert forked._websocket is None
            assert forked._response_chain is not provider._response_chain
    finally:
        forked.close()
        provider.close()


@pytest.mark.parametrize(
    "prefix", ["", "data: ", '{"type":"response.output_text.delta","delta":"partial"}']
)
def test_websocket_timeout_replay_stops_after_first_frame(
    prefix: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from yoke.ai.providers.codex.websocket.config import CodexWebSocketTimeoutError

    attempts = 0
    provider = _provider("codex-ws", tmp_path, None)

    class Socket:
        delivered = False

        def recv(self, timeout: float | None = None) -> str:
            if prefix and not self.delivered:
                self.delivered = True
                return prefix
            raise TimeoutError

        def send(self, data: str) -> None:
            pass

        def close(self) -> None:
            pass

    socket = Socket()

    def fresh() -> Socket:
        nonlocal attempts
        attempts += 1
        return socket

    monkeypatch.setattr(provider, "_fresh_websocket", fresh)
    # Force the idle deadline to expire on the next loop without sleeping.
    ticks = iter(range(1000))
    monkeypatch.setattr(
        "yoke.ai.providers.codex.websocket.transport.time.monotonic",
        lambda: next(ticks),
    )
    provider.config.timeout_seconds = 1.5
    try:
        with pytest.raises(
            (ProviderError, CodexWebSocketTimeoutError),
            match="(timed out|response bytes)",
        ):
            provider.complete([Message.user("hello")], [])
        assert attempts == (1 if prefix else 2)
    finally:
        provider.close()


def test_codex_http_fork_keeps_original_fresh_routing_semantics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tokens = iter(("first", "second", "third"))
    monkeypatch.setattr("secrets.token_hex", lambda _n: next(tokens))
    provider = _provider("codex-http", tmp_path, None)
    provider.config.prompt_cache_key = None
    provider._prompt_cache_key = provider._new_prompt_cache_key()
    provider._turn_state = "parent-route"
    forked = cast(Any, fork_provider(provider))
    try:
        assert forked._turn_state is None
        assert forked._prompt_cache_key != provider._prompt_cache_key
    finally:
        forked.close()
        provider.close()
