from __future__ import annotations

# ruff: noqa: D100, D103, E501, S101

import base64
import json
from pathlib import Path
from typing import cast

import httpx

from yoke.agent.context import ContextManager
from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import Message
from yoke.agent.models import MessageImageURL
from yoke.agent.models import MessageImageURLContentPart
from yoke.agent.models import MessageLocalImageContentPart
from yoke.agent.models import MessageTextContentPart
from yoke.agent.multimodal import omit_image_inputs_for_text_model
from yoke.agent.multimodal import messages_for_provider_capabilities
from yoke.agent.tools import AttachImageTool
from yoke.ai.providers.base import ProviderModelInfo
from yoke.ai.providers.openai_compat import OpenAICompatibleConfig
from yoke.ai.providers.openai_compat import OpenAICompatibleProvider
from yoke.ai.providers.openai_compat import serialize_message_for_openai
from yoke.cli.image_input import next_image_label_index


TINY_PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO7Z0mQAAAAASUVORK5CYII="


def assert_serialized_image_envelope(
    content: list[dict[str, object]],
    *,
    text: str,
    image_name: str,
) -> None:
    assert content[0] == {"type": "text", "text": text}
    assert content[1] == {
        "type": "text",
        "text": f"<image name={image_name}>",
    }
    assert content[2]["type"] == "image_url"
    image_url = cast(dict[str, object], content[2]["image_url"])
    assert "detail" not in content[2]
    assert image_url["detail"] == "high"
    assert content[3] == {"type": "text", "text": "</image>"}


def test_attach_image_tool_emits_deferred_multimodal_context_message(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "proof.png"
    image_path.write_bytes(base64.b64decode(TINY_PNG))
    context_manager = ContextManager()
    context = context_manager.initialize("compare this")

    tool = AttachImageTool.bind(root=tmp_path, messages=list(context.messages))
    invocation = tool.parse_arguments(
        {"path": str(image_path), "caption": "Compare [Image #1]."}
    )

    result = invocation.execute()
    invocation.apply_result(context, result)
    pending = invocation.pending_context_messages(result)

    assert result["ok"] is True
    assert result["label"] == "[Image #1]"
    assert len(pending) == 1
    appended = pending[0]
    assert isinstance(appended.content, list)
    assert appended.content[0] == MessageTextContentPart(text="Compare [Image #1].")
    assert appended.content[1] == MessageLocalImageContentPart(
        path=str(image_path.resolve()),
        label="[Image #1]",
    )


def test_attach_image_tool_continues_label_sequence() -> None:
    messages = [
        Message.user(
            [
                MessageTextContentPart(text="Earlier image"),
                MessageLocalImageContentPart(
                    path="C:/tmp/one.png",
                    label="[Image #1]",
                ),
            ]
        ),
        Message.user(
            [
                MessageLocalImageContentPart(
                    path="C:/tmp/two.png",
                    label="[Image #2]",
                )
            ]
        ),
    ]

    assert next_image_label_index(messages) == 3


def test_attach_image_tool_accepts_external_absolute_path(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "external.png"
    image_path.write_bytes(base64.b64decode(TINY_PNG))

    tool = AttachImageTool.bind(root=Path.cwd(), messages=[])
    invocation = tool.parse_arguments({"path": str(image_path)})
    result = invocation.execute()

    assert result["ok"] is True
    assert result["path"] == str(image_path.resolve())


def test_attach_image_serializes_like_normal_multimodal_message(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "payload.png"
    image_path.write_bytes(base64.b64decode(TINY_PNG))
    message = Message.user(
        [
            MessageTextContentPart(text="Inspect [Image #1]."),
            MessageLocalImageContentPart(
                path=str(image_path),
                label="[Image #1]",
            ),
        ]
    )

    payload = serialize_message_for_openai(message)
    content = cast(list[dict[str, object]], payload["content"])

    assert_serialized_image_envelope(
        content,
        text="Inspect [Image #1].",
        image_name="[Image #1]",
    )


def test_omit_image_inputs_for_text_model_preserves_history() -> None:
    message = Message.user(
        [
            MessageTextContentPart(text="Compare these."),
            MessageLocalImageContentPart(
                path="C:/tmp/one.png",
                label="[Image #1]",
            ),
            MessageImageURLContentPart(
                image_url=MessageImageURL(url="https://example.test/two.png")
            ),
        ]
    )

    projected = omit_image_inputs_for_text_model([message])

    assert message.has_image_inputs()
    assert not projected[0].has_image_inputs()
    projected_text = projected[0].text_content() or ""
    assert "Compare these." in projected_text
    assert "[Image omitted: [Image #1]" in projected_text
    assert "[Image omitted: [Image]" in projected_text


def test_provider_capability_projection_prefers_current_model_info() -> None:
    class MixedCapabilityProvider:
        supports_image_inputs = True

        def current_model_info(self) -> ProviderModelInfo:
            return ProviderModelInfo(
                id="deepseek-v4-pro",
                display_name="DeepSeek V4 Pro",
                context_window_tokens=1_000_000,
                thinking_levels=("low",),
                supports_image_inputs=False,
            )

    message = Message.user(
        [
            MessageTextContentPart(text="Earlier screenshot."),
            MessageLocalImageContentPart(
                path="C:/tmp/one.png",
                label="[Image #1]",
            ),
        ]
    )

    projected = messages_for_provider_capabilities([message], MixedCapabilityProvider())

    assert message.has_image_inputs()
    assert not projected[0].has_image_inputs()
    assert "[Image omitted: [Image #1]" in (projected[0].text_content() or "")


def test_agent_tool_call_attaches_image_for_following_provider_turn(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "followup.png"
    image_path.write_bytes(base64.b64decode(TINY_PNG))
    payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        payloads.append(payload)
        if len(payloads) == 1:
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "type": "function",
                                        "function": {
                                            "name": "attach_image",
                                            "arguments": json.dumps(
                                                {
                                                    "path": str(image_path.resolve()),
                                                    "caption": (
                                                        "Analyze [Image #1] "
                                                        "before answering."
                                                    ),
                                                }
                                            ),
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "I can now see the image.",
                        }
                    }
                ]
            },
        )

    provider = OpenAICompatibleProvider(
        OpenAICompatibleConfig(
            api_key="test-key",
            model="gpt-test",
            base_url="http://unit-test.local",
        ),
        http_client=httpx.Client(
            transport=httpx.MockTransport(handler),
            base_url="http://unit-test.local",
        ),
    )
    agent = RuntimeAgent(
        provider=provider,
        tools=[AttachImageTool.bind(root=tmp_path)],
        context_manager=ContextManager(),
    )
    try:
        result = agent.run("Load the screenshot if needed.")
    finally:
        provider.close()

    assert result.output == "I can now see the image."
    assert len(payloads) == 2
    second_messages = cast(list[dict[str, object]], payloads[1]["messages"])
    assert second_messages[0] == {
        "role": "user",
        "content": "Load the screenshot if needed.",
    }
    tool_message = second_messages[1]
    assert tool_message["role"] == "assistant"
    attached_user_message = next(
        message
        for message in second_messages
        if message["role"] == "user" and isinstance(message.get("content"), list)
    )
    attached_content = cast(list[dict[str, object]], attached_user_message["content"])
    assert attached_content[0] == {
        "type": "text",
        "text": "Analyze [Image #1] before answering.",
    }
    assert attached_content[1] == {
        "type": "text",
        "text": "<image name=[Image #1]>",
    }
    assert attached_content[2]["type"] == "image_url"
    assert attached_content[3] == {"type": "text", "text": "</image>"}
