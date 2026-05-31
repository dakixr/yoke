from __future__ import annotations

# ruff: noqa: F403, F405
from typing import cast

from .support import *  # noqa: F403, F405

from yoke.ai.providers.openai_compat import serialize_message_for_openai


def test_openai_serializer_wraps_multiple_images_with_stable_labels() -> None:
    message = Message.user(
        [
            MessageTextContentPart(text="Compare [Image #1] and [Image #2]."),
            MessageLocalImageContentPart(
                path="C:/tmp/first.png",
                label="[Image #1]",
            ),
            MessageLocalImageContentPart(
                path="C:/tmp/second.png",
                label="[Image #2]",
            ),
        ]
    )

    original = serialize_message_for_openai.__globals__["_local_image_to_data_url"]
    serialize_message_for_openai.__globals__["_local_image_to_data_url"] = lambda path: (
        f"data:image/png;base64,{Path(path).stem}"
    )
    try:
        payload = serialize_message_for_openai(message)
    finally:
        serialize_message_for_openai.__globals__["_local_image_to_data_url"] = original

    assert payload["role"] == "user"
    assert payload["content"] == [
        {"type": "text", "text": "Compare [Image #1] and [Image #2]."},
        {"type": "text", "text": "<image name=[Image #1]>"},
        {
            "type": "image_url",
            "image_url": {
                "url": "data:image/png;base64,first",
                "detail": "high",
            },
        },
        {"type": "text", "text": "</image>"},
        {"type": "text", "text": "<image name=[Image #2]>"},
        {
            "type": "image_url",
            "image_url": {
                "url": "data:image/png;base64,second",
                "detail": "high",
            },
        },
        {"type": "text", "text": "</image>"},
    ]


def test_headless_cli_sends_multimodal_user_message_to_provider(
    tmp_path: Path,
) -> None:
    import httpx

    from yoke.agent.context import ContextManager
    from yoke.agent.loop import RuntimeAgent
    from yoke.ai.providers.openai_compat import OpenAICompatibleConfig
    from yoke.ai.providers.openai_compat import OpenAICompatibleProvider

    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "I received the image payload.",
                        }
                    }
                ]
            },
        )

    image_path = tmp_path / "yoke-manual-headless-proof.png"
    image_path.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO7Z0mQAAAAASUVORK5CYII="
        )
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
        tools=[],
        context_manager=ContextManager(),
    )
    try:
        exit_code = run_cli(
            CLIArgs(
                prompt=(
                    "tell me what image is attached [yoke-manual-headless-proof.png]"
                ),
                headless=True,
                root=str(tmp_path),
                images=(str(image_path),),
            ),
            agent=agent,
        )
    finally:
        provider.close()

    assert exit_code == 0
    payload = cast(dict[str, object], captured["payload"])
    messages = cast(list[object], payload["messages"])
    assert len(messages) == 1
    user_message = cast(dict[str, object], messages[0])
    assert user_message["role"] == "user"
    content = cast(list[object], user_message["content"])
    assert content[0] == {
        "type": "text",
        "text": "tell me what image is attached [yoke-manual-headless-proof.png]",
    }
    assert content[1] == {
        "type": "text",
        "text": "<image name=[Image #1]>",
    }
    image_part = cast(dict[str, object], content[2])
    image_url = cast(dict[str, object], image_part["image_url"])
    assert image_part["type"] == "image_url"
    assert "detail" not in image_part
    assert image_url["detail"] == "high"
    assert cast(str, image_url["url"]).startswith("data:image/png;base64,")
    assert content[3] == {"type": "text", "text": "</image>"}
