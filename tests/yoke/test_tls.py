from __future__ import annotations

# ruff: noqa: S101

import ssl
from pathlib import Path
from typing import Any

import httpx
import pytest

from yoke._tls import tls_verification_enabled
from yoke.ai.providers import OpenAICompatibleConfig, OpenAICompatibleProvider
from yoke.ai.providers.codex.subscription import (
    CodexSubscriptionConfig,
    CodexSubscriptionProvider,
)
from yoke.ai.providers.codex.websocket.config import ssl_context_for_websocket_url
from yoke.ai.providers.opencode_go import OpenCodeGoConfig, OpenCodeGoProvider
from yoke.ai.providers.zai import ZAIConfig, ZAIProvider
from yoke.mcp.client import StreamableHttpClient
from yoke.mcp.config import McpServerConfig


@pytest.mark.parametrize("value", ["0", "false", "NO", " off "])
def test_disable_tls_false_values_keep_verification_enabled(value: str) -> None:
    assert tls_verification_enabled({"YOKE_DISABLE_TLS": value}) is True


@pytest.mark.parametrize("value", ["", "1", "true", "yes", "on", "anything"])
def test_disable_tls_set_values_disable_verification(value: str) -> None:
    assert tls_verification_enabled({"YOKE_DISABLE_TLS": value}) is False


def test_tls_verification_is_enabled_by_default() -> None:
    assert tls_verification_enabled({}) is True


def test_codex_websocket_disables_certificate_and_hostname_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("YOKE_DISABLE_TLS", "1")

    context = ssl_context_for_websocket_url("wss://example.test/v1/responses")

    assert context is not None
    assert context.check_hostname is False
    assert context.verify_mode == ssl.CERT_NONE


def test_owned_provider_clients_disable_tls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("YOKE_DISABLE_TLS", "1")
    client_options: list[dict[str, Any]] = []

    class FakeClient:
        def close(self) -> None:
            return None

    def create_client(**options: Any) -> FakeClient:
        client_options.append(options)
        return FakeClient()

    monkeypatch.setattr(httpx, "Client", create_client)

    openai = OpenAICompatibleProvider(
        OpenAICompatibleConfig(api_key="test", model="test")
    )
    zai = ZAIProvider(ZAIConfig(api_key="test"))
    opencode = OpenCodeGoProvider(
        OpenCodeGoConfig(api_key="test", session_id="session")
    )
    codex = CodexSubscriptionProvider(
        CodexSubscriptionConfig(
            auth_path=tmp_path / "auth.json",
            accounts_dir=tmp_path / "accounts",
            auths_path=tmp_path / "auths.json",
            selection_path=tmp_path / "selection.json",
        )
    )

    try:
        openai._client.get_client()
        opencode._openai_provider._client.get_client()
        assert len(client_options) == 5
        assert all(options["verify"] is False for options in client_options)
    finally:
        openai.close()
        zai.close()
        opencode.close()
        codex.close()


def test_remote_mcp_tls_setting_is_overridden_by_global_disable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("YOKE_DISABLE_TLS", "1")
    transport_options: dict[str, object] = {}

    def create_transport(**options: object) -> object:
        transport_options.update(options)
        return object()

    monkeypatch.setattr(httpx, "HTTPTransport", create_transport)
    monkeypatch.setattr(httpx, "Client", lambda **_options: object())

    StreamableHttpClient(
        McpServerConfig(
            name="sample",
            transport="streamable-http",
            url="https://mcp.example.test",
            verify=True,
        ),
        root=tmp_path,
    )

    assert transport_options["verify"] is False
