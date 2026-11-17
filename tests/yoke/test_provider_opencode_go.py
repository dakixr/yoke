# ruff: noqa: D100,D103,S101

from __future__ import annotations

import json
from typing import cast

import httpx
import pytest

from yoke.agent.models import Message
from yoke.ai.providers.base import fork_provider
from yoke.ai.providers.opencode_go import OpenCodeGoConfig
from yoke.ai.providers.opencode_go import OpenCodeGoProvider


def test_opencode_go_muse_spark_contributor_catalog_and_request() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "muse-ok"}],
                    }
                ]
            },
        )

    provider = OpenCodeGoProvider(
        OpenCodeGoConfig(
            api_key="test",
            model="muse-spark-1.3-contributor",
        ),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    try:
        model = provider.current_model_info()
        message = provider.complete([Message.user("hello")], [])
    finally:
        provider.close()

    payload = cast(dict[str, object], captured["payload"])
    assert model is not None
    assert model.context_window_tokens == 400_000
    assert model.thinking_levels == ("minimal", "low", "medium", "high", "xhigh")
    assert model.default_thinking_level == "high"
    assert model.supports_image_inputs is True
    assert [item.id for item in provider.list_models()] == [
        "muse-spark-1.3-contributor",
        "deepseek-v4.1-flash",
    ]
    deepseek_41 = provider.list_models()[1]
    assert deepseek_41.display_name == "DeepSeek V4.1 Flash"
    assert deepseek_41.context_window_tokens == 400_000
    assert deepseek_41.thinking_levels == ("low", "high", "max")
    assert deepseek_41.default_thinking_level == "high"
    assert deepseek_41.supports_image_inputs is True
    assert captured["url"] == "https://opencode.ai/zen/go/v1/responses"
    assert payload["model"] == "muse-spark-1.3-contributor"
    assert payload["reasoning"] == {
        "effort": "high",
        "summary": "auto",
        "context": "all_turns",
    }
    assert message.content == "muse-ok"

    minimal_provider = OpenCodeGoProvider(
        OpenCodeGoConfig(
            api_key="test",
            model="muse-spark-1.3-contributor",
            reasoning_effort="minimal",
        ),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    try:
        assert minimal_provider.config.reasoning_effort == "minimal"
    finally:
        minimal_provider.close()


def test_opencode_go_deepseek_41_flash_uses_chat_completions() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["session"] = request.headers["x-opencode-session"]
        captured["payload"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            text=_chat_stream("deepseek-ok"),
        )

    provider = OpenCodeGoProvider(
        OpenCodeGoConfig(
            api_key="test",
            model="deepseek-v4.1-flash",
            reasoning_effort="low",
        ),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    try:
        message = provider.complete([Message.user("hello")], [])
    finally:
        provider.close()

    payload = cast(dict[str, object], captured["payload"])
    assert captured["url"] == "https://opencode.ai/zen/go/v1/chat/completions"
    assert captured["session"] == provider.config.session_id
    assert payload["model"] == "deepseek-v4.1-flash"
    assert payload["reasoning_effort"] == "low"
    assert payload["stream"] is True
    assert message.content == "deepseek-ok"


def test_opencode_go_generated_session_id_is_stable_across_requests() -> None:
    sessions: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sessions.append(request.headers["x-opencode-session"])
        return httpx.Response(
            200,
            text=_chat_stream("ok"),
        )

    provider = OpenCodeGoProvider(
        OpenCodeGoConfig(api_key="test", model="deepseek-v4.1-flash"),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    try:
        provider.complete([Message.user("first")], [])
        provider.complete([Message.user("second")], [])
    finally:
        provider.close()

    assert sessions == [provider.config.session_id, provider.config.session_id]
    assert provider.config.session_id


def test_opencode_go_session_id_survives_provider_fork() -> None:
    provider = OpenCodeGoProvider(
        OpenCodeGoConfig(
            api_key="test",
            model="deepseek-v4.1-flash",
            session_id="conversation-fork",
        )
    )
    forked = cast(OpenCodeGoProvider, fork_provider(provider))

    try:
        assert forked is not provider
        assert forked.config.session_id == "conversation-fork"
    finally:
        forked.close()
        provider.close()


def test_opencode_go_can_rebind_session_id() -> None:
    sessions: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sessions.append(request.headers["x-opencode-session"])
        return httpx.Response(
            200,
            text=_chat_stream("ok"),
        )

    provider = OpenCodeGoProvider(
        OpenCodeGoConfig(
            api_key="test",
            model="deepseek-v4.1-flash",
            session_id="source-session",
        ),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    try:
        provider.complete([Message.user("source")], [])
        provider.set_session_id("fork-session")
        provider.complete([Message.user("fork")], [])
    finally:
        provider.close()

    assert sessions == ["source-session", "fork-session"]


def _chat_stream(text: str) -> str:
    return (
        "data: "
        + json.dumps(
            {
                "choices": [
                    {"index": 0, "delta": {"content": text}, "finish_reason": "stop"}
                ]
            }
        )
        + "\n\ndata: [DONE]\n\n"
    )


def _capture_provider(
    model: str,
    captured: list[dict[str, object]],
    *,
    base_url: str = "https://opencode.ai/zen/go/v1",
    model_prefix: str = "",
) -> OpenCodeGoProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        captured.append({"url": str(request.url), "model": payload["model"]})
        if request.url.path.endswith("/responses"):
            return httpx.Response(
                200,
                json={
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "ok"}],
                        }
                    ]
                },
            )
        return httpx.Response(200, text=_chat_stream("ok"))

    return OpenCodeGoProvider(
        OpenCodeGoConfig(
            api_key="test",
            model=model,
            base_url=base_url,
            model_prefix=model_prefix,
        ),
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


@pytest.mark.parametrize(
    "model,path",
    [
        ("deepseek-v4.1-flash", "chat/completions"),
        ("muse-spark-1.3-contributor", "responses"),
    ],
)
def test_opencode_go_defaults_to_direct_endpoint_and_bare_model(
    model: str, path: str
) -> None:
    captured: list[dict[str, object]] = []
    provider = _capture_provider(model, captured)
    try:
        provider.complete([Message.user("hello")], [])
    finally:
        provider.close()
    assert captured == [
        {"url": f"https://opencode.ai/zen/go/v1/{path}", "model": model}
    ]


@pytest.mark.parametrize(
    "model,path",
    [
        ("deepseek-v4.1-flash", "chat/completions"),
        ("muse-spark-1.3-contributor", "responses"),
    ],
)
def test_opencode_go_gateway_base_url_and_upstream_model_prefix(
    model: str, path: str
) -> None:
    captured: list[dict[str, object]] = []
    provider = _capture_provider(
        model,
        captured,
        base_url="http://fleet-spain:8317/v1/",
        model_prefix="opencode-go",
    )
    try:
        provider.complete([Message.user("hello")], [])
        forked = cast(OpenCodeGoProvider, fork_provider(provider))
        assert forked.config.base_url == "http://fleet-spain:8317/v1"
        assert forked.config.model_prefix == "opencode-go"
    finally:
        provider.close()
    assert captured == [
        {"url": f"http://fleet-spain:8317/v1/{path}", "model": f"opencode-go/{model}"}
    ]
    # Yoke's own model ID stays unprefixed for sessions and selections.
    assert provider.current_model_id() == model


def test_opencode_go_register_provider_reads_gateway_environment() -> None:
    from types import SimpleNamespace

    from yoke.ai.providers.opencode_go.catalog import register_provider

    def provider_for(env: dict[str, str]) -> OpenCodeGoProvider:
        return register_provider(
            SimpleNamespace(
                env={"OPENCODE_API_KEY": "tailscale", **env},
                model="opencode-go/deepseek-v4.1-flash",
                session_id="s",
                reasoning_effort=None,
            )
        )

    direct = provider_for({})
    gateway = provider_for(
        {
            "YOKE_OPENCODE_GO_BASE_URL": "http://fleet-spain:8317/v1",
            "YOKE_OPENCODE_GO_MODEL_PREFIX": "opencode-go",
        }
    )
    try:
        assert direct.config.base_url == "https://opencode.ai/zen/go/v1"
        assert direct.config.model_prefix == ""
        assert gateway.config.base_url == "http://fleet-spain:8317/v1"
        assert gateway.config.upstream_model("deepseek-v4.1-flash") == (
            "opencode-go/deepseek-v4.1-flash"
        )
        assert gateway.config.model == "deepseek-v4.1-flash"
    finally:
        direct.close()
        gateway.close()
