# ruff: noqa: D100, D103, S101

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any, cast

import httpx

from yoke.ai.providers.codex_subscription import CodexSubscriptionConfig
from yoke.ai.providers.codex_subscription import CodexSubscriptionProvider
from yoke.ai.providers.codex_subscription import OAuthCredentials


TINY_PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO7Z0mQAAAAASUVORK5CYII="


def test_codex_provider_generate_image_posts_to_subscription_endpoint(
    tmp_path: Path,
) -> None:
    class TestCodexProvider(CodexSubscriptionProvider):
        def _fresh_credentials(self) -> OAuthCredentials:
            return OAuthCredentials(
                access="access-token",
                refresh="refresh-token",
                expires=9999999999999,
                account_id="account-id",
            )

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"created": 1, "data": [{"b64_json": TINY_PNG}]},
        )

    provider = TestCodexProvider(
        CodexSubscriptionConfig(
            auth_path=tmp_path / "auth.json",
            accounts_dir=tmp_path / "accounts",
            auths_path=tmp_path / "auths.json",
            selection_path=tmp_path / "selection.json",
            base_url="https://chatgpt.com/backend-api",
        ),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    try:
        encoded = provider.generate_image(prompt="a small fox")
    finally:
        provider.close()

    assert base64.b64decode(encoded) == base64.b64decode(TINY_PNG)
    assert len(requests) == 1
    request = requests[0]
    assert (
        str(request.url) == "https://chatgpt.com/backend-api/codex/images/generations"
    )
    assert request.headers["Authorization"] == "Bearer access-token"
    assert request.headers["chatgpt-account-id"] == "account-id"
    payload = cast(dict[str, Any], json.loads(request.content.decode("utf-8")))
    assert payload == {
        "prompt": "a small fox",
        "background": "auto",
        "model": "gpt-image-2",
        "quality": "auto",
        "size": "auto",
    }


def test_codex_provider_edit_image_posts_to_subscription_endpoint(
    tmp_path: Path,
) -> None:
    class TestCodexProvider(CodexSubscriptionProvider):
        def _fresh_credentials(self) -> OAuthCredentials:
            return OAuthCredentials(
                access="access-token",
                refresh="refresh-token",
                expires=9999999999999,
                account_id="account-id",
            )

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"created": 1, "data": [{"b64_json": TINY_PNG}]},
        )

    provider = TestCodexProvider(
        CodexSubscriptionConfig(
            auth_path=tmp_path / "auth.json",
            accounts_dir=tmp_path / "accounts",
            auths_path=tmp_path / "auths.json",
            selection_path=tmp_path / "selection.json",
            base_url="https://chatgpt.com/backend-api",
        ),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    try:
        encoded = provider.edit_image(
            prompt="add a hat",
            image_urls=["data:image/png;base64,Zm9v"],
        )
    finally:
        provider.close()

    assert base64.b64decode(encoded) == base64.b64decode(TINY_PNG)
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == "https://chatgpt.com/backend-api/codex/images/edits"
    assert request.headers["Authorization"] == "Bearer access-token"
    payload = cast(dict[str, Any], json.loads(request.content.decode("utf-8")))
    assert payload == {
        "images": [{"image_url": "data:image/png;base64,Zm9v"}],
        "prompt": "add a hat",
        "background": "auto",
        "model": "gpt-image-2",
        "quality": "auto",
        "size": "auto",
    }
