"""Byte contracts captured at 107c74c before provider consolidation."""

# ruff: noqa: S101
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from websockets.protocol import State

from yoke.agent.models import Message, ToolCall, ToolFunction
from yoke.ai.providers.openai_compat import (
    OpenAICompatibleConfig,
    OpenAICompatibleProvider,
)
from yoke.ai.providers.opencode_go import OpenCodeGoConfig, OpenCodeGoProvider
from yoke.ai.providers.zai import ZAIConfig, ZAIProvider
from yoke.ai.providers.codex.subscription import (
    CodexSubscriptionConfig,
    CodexSubscriptionProvider,
    OAuthCredentials,
)
from yoke.ai.providers.codex.websocket.provider import CodexProvider
from yoke.ai.providers.codex.websocket.config import CodexConfig

FIXTURES = Path(__file__).with_name("fixtures") / "request_contract.json"
KINDS = ("openai", "zai", "go-chat", "go-responses", "codex-http", "codex-ws")


def _messages(rich: bool) -> list[Message]:
    if not rich:
        return [Message.system("System.\nKeep order."), Message.user("Hello ☃")]
    return [
        Message.system("System.\nKeep order."),
        Message.system("Skill instructions."),
        Message.user("Read the file"),
        Message(
            role="assistant",
            content="Checking",
            reasoning_content="Private thought",
            tool_calls=[
                ToolCall(
                    id="call_1",
                    function=ToolFunction(name="read", arguments='{"path":"x"}'),
                )
            ],
        ),
        Message.tool("call_1", "file content\n<checkpoint>"),
        Message.user(
            [
                {"type": "text", "text": "Inspect"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,AAAA"},
                    "detail": "high",
                },
            ]
        ),
    ]


class _Socket:
    state = State.OPEN

    def __init__(self, captured: list[dict[str, object]]) -> None:
        self.captured = captured

    def send(self, payload: str) -> None:
        self.captured.append({"body": payload})

    def recv(self, timeout: float | None = None) -> str:
        return json.dumps(
            {
                "type": "response.completed",
                "response": {
                    "id": "resp-fixed",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "ok"}],
                        }
                    ],
                },
            }
        )

    def close(self) -> None:
        pass


def capture(
    kind: str, rich: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> list[dict[str, object]]:
    """Capture actual outbound bytes, with deterministic environment metadata."""
    captured: list[dict[str, object]] = []
    monkeypatch.setattr("secrets.token_hex", lambda _n: "fixed-id")
    monkeypatch.setattr("platform.system", lambda: "Linux")
    monkeypatch.setattr("platform.machine", lambda: "x86_64")
    monkeypatch.setattr(
        CodexSubscriptionProvider,
        "_fresh_credentials",
        lambda _self: OAuthCredentials(
            access="test",
            refresh="test",
            expires=4102444800000,
            account_id="acct-fixed",
        ),
    )
    monkeypatch.setattr(
        CodexSubscriptionProvider, "_log_event", lambda *_args, **_kwargs: None
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(
            {
                "url": str(request.url),
                "headers": dict(request.headers),
                "body": request.content.decode(),
            }
        )
        if kind == "openai":
            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]},
            )
        if kind == "go-responses":
            return httpx.Response(
                200,
                json={
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "ok"}],
                        }
                    ]
                },
            )
        if kind == "codex-http":
            event = {
                "type": "response.completed",
                "response": {
                    "id": "resp-fixed",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "ok"}],
                        }
                    ],
                },
            }
        else:
            event = {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}
        return httpx.Response(
            200,
            content=f"data: {json.dumps(event)}\n\ndata: [DONE]\n\n",
            headers={"content-type": "text/event-stream"},
        )

    client = httpx.Client(
        transport=httpx.MockTransport(handler), headers={"User-Agent": "contract-test"}
    )
    common: dict[str, Any] = {"http_client": client, "sleep": lambda _s: None}
    auth: dict[str, Any] = {
        "auth_path": tmp_path / "auth",
        "accounts_dir": tmp_path / "accounts",
        "auths_path": tmp_path / "auths",
        "selection_path": tmp_path / "selection",
        "logs_dir": tmp_path / "logs",
        "prompt_cache_key": "cache-fixed",
        "max_retries": 0,
    }
    provider: Any
    if kind == "openai":
        provider = OpenAICompatibleProvider(
            OpenAICompatibleConfig(
                api_key="test",
                model="model",
                reasoning_effort="high",
                max_tokens=123,
                headers={"x-session": "session-fixed"},
            ),
            **common,
        )
    elif kind == "zai":
        provider = ZAIProvider(
            ZAIConfig(api_key="test", reasoning_effort="high"), **common
        )
    elif kind.startswith("go-"):
        model = (
            "deepseek-v4.1-flash" if kind == "go-chat" else "muse-spark-1.3-contributor"
        )
        provider = OpenCodeGoProvider(
            OpenCodeGoConfig(api_key="test", model=model, session_id="session-fixed"),
            **common,
        )
    elif kind == "codex-http":
        provider = CodexSubscriptionProvider(CodexSubscriptionConfig(**auth), **common)
    else:

        def factory(url: str, **kwargs: Any) -> _Socket:
            captured.append({"url": url, "headers": kwargs["additional_headers"]})
            return _Socket(captured)

        provider = CodexProvider(
            CodexConfig(**auth), websocket_factory=factory, sleep=lambda _s: None
        )
    tools: list[dict[str, object]] = (
        [
            {
                "type": "function",
                "function": {
                    "name": "read",
                    "description": "Read a file",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                },
            }
        ]
        if rich
        else []
    )
    try:
        messages = _messages(rich)
        provider.complete(messages, tools)
        # A follow-up exercises session affinity, turn state and WS continuation.
        if rich:
            setattr(provider, "_turn_state", "turn-fixed")
            provider.complete(
                [*messages, Message.assistant("ok"), Message.user("Continue")], tools
            )
    finally:
        provider.close()
        client.close()
    return captured


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("rich", [False, True], ids=["text", "tools-images-reasoning"])
def test_original_request_bytes(
    kind: str, rich: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Keep outgoing JSON bytes, header values, cache scope and order unchanged."""
    expected = json.loads(FIXTURES.read_text())
    assert capture(kind, rich, tmp_path, monkeypatch) == expected[f"{kind}:{rich}"]
