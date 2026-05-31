# ruff: noqa

from __future__ import annotations

from pathlib import Path

from yoke.ai import build_user_message
from yoke.ai import complete
from yoke.ai import image_part
from yoke.ai import remote_image_part
from yoke.ai import text_part
from yoke.agent.models import Message
from yoke.agent.models import MessageImageURL
from yoke.agent.models import MessageImageURLContentPart
from yoke.agent.models import MessageLocalImageContentPart
from yoke.agent.models import MessageTextContentPart


class RecordingProvider:
    supports_image_inputs = True
    max_images_per_message = None

    def __init__(self) -> None:
        self.messages: list[Message] | None = None
        self.tools: list[dict[str, object]] | None = None

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        self.messages = [message.model_copy(deep=True) for message in messages]
        self.tools = list(tools)
        return Message.assistant("done")


def test_build_user_message_assigns_stable_labels() -> None:
    message = build_user_message(
        "Compare [Image #1] and [Image #2].",
        images=["first.png", "second.png"],
    )

    assert isinstance(message.content, list)
    assert message.content == [
        MessageTextContentPart(text="Compare [Image #1] and [Image #2]."),
        MessageLocalImageContentPart(
            path=str(Path("first.png").expanduser().resolve()),
            label="[Image #1]",
        ),
        MessageLocalImageContentPart(
            path=str(Path("second.png").expanduser().resolve()),
            label="[Image #2]",
        ),
    ]


def test_complete_accepts_images_shortcut() -> None:
    provider = RecordingProvider()

    result = complete(
        provider=provider,
        prompt="Describe [Image #1].",
        images=["photo.png"],
    )

    assert result.output == "done"
    assert provider.messages is not None
    message = provider.messages[-1]
    assert isinstance(message.content, list)
    assert message.content == [
        MessageTextContentPart(text="Describe [Image #1]."),
        MessageLocalImageContentPart(
            path=str(Path("photo.png").expanduser().resolve()),
            label="[Image #1]",
        ),
    ]


def test_complete_accepts_remote_image_shortcut() -> None:
    provider = RecordingProvider()

    complete(
        provider=provider,
        prompt="Describe the remote image.",
        image_urls=["data:image/png;base64,abc"],
    )

    assert provider.messages is not None
    message = provider.messages[-1]
    assert isinstance(message.content, list)
    assert message.content == [
        MessageTextContentPart(text="Describe the remote image."),
        MessageImageURLContentPart(
            image_url=MessageImageURL(url="data:image/png;base64,abc")
        ),
    ]


def test_sdk_helper_builders_remain_available() -> None:
    assert text_part("hello") == MessageTextContentPart(text="hello")
    assert image_part("shot.png", label="[Image #9]") == (
        MessageLocalImageContentPart(
            path=str(Path("shot.png").expanduser().resolve()),
            label="[Image #9]",
        )
    )
    assert remote_image_part("data:image/png;base64,abc") == (
        MessageImageURLContentPart(
            image_url=MessageImageURL(url="data:image/png;base64,abc")
        )
    )
