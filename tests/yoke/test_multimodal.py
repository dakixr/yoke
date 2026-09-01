from __future__ import annotations

# ruff: noqa: D100, D103, S101

from yoke.agent.models import Message
from yoke.agent.models import MessageImageURL
from yoke.agent.models import MessageImageURLContentPart
from yoke.agent.models import MessageTextContentPart
from yoke.agent.multimodal import messages_for_provider_capabilities


class _TwoImageProvider:
    supports_image_inputs = True
    max_images_per_request = 2


def _image_message(index: int) -> Message:
    return Message.user(
        [
            MessageTextContentPart(text=f"image {index}"),
            MessageImageURLContentPart(
                image_url=MessageImageURL(url=f"data:image/png;base64,{index}"),
                label=f"[Image #{index}]",
            ),
        ]
    )


def test_provider_request_image_budget_keeps_newest_images() -> None:
    original = [_image_message(1), _image_message(2), _image_message(3)]

    projected = messages_for_provider_capabilities(original, _TwoImageProvider())

    assert isinstance(projected[0].content, list)
    assert isinstance(projected[0].content[1], MessageTextContentPart)
    assert "at most 2 images per request" in projected[0].content[1].text
    assert isinstance(projected[1].content, list)
    assert isinstance(projected[1].content[1], MessageImageURLContentPart)
    assert isinstance(projected[2].content, list)
    assert isinstance(projected[2].content[1], MessageImageURLContentPart)

    assert isinstance(original[0].content, list)
    assert isinstance(original[0].content[1], MessageImageURLContentPart)
