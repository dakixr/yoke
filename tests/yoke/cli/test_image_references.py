from __future__ import annotations

from pathlib import Path

from yoke.agent.models import Message
from yoke.agent.models import MessageLocalImageContentPart
from yoke.agent.models import MessageTextContentPart
from prompt_toolkit.buffer import Buffer

from yoke.cli.image_input import ImageAttachment
from yoke.cli.image_input import attach_standalone_prompt_image_paths
from yoke.cli.image_input import build_user_message
from yoke.cli.interactive.prompt_keys import insert_attachment_reference


def test_build_user_message_keeps_attachment_reference_text() -> None:
    message = build_user_message(
        "Please inspect [screenshot.png] carefully.",
        image_paths=[Path("C:/tmp/screenshot.png")],
    )

    assert isinstance(message.content, list)
    assert message.content == [
        MessageTextContentPart(text="Please inspect [screenshot.png] carefully."),
        MessageLocalImageContentPart(
            path=str(Path("C:/tmp/screenshot.png").resolve()),
            label="[Image #1]",
        ),
    ]
    assert message.text_content() == "Please inspect [screenshot.png] carefully."


def test_build_user_message_assigns_stable_labels_for_multiple_images() -> None:
    message = build_user_message(
        "Compare [Image #1] and [Image #2].",
        image_paths=[
            Path("C:/tmp/first.png"),
            Path("C:/tmp/second.png"),
        ],
    )

    assert isinstance(message.content, list)
    assert message.content[1] == MessageLocalImageContentPart(
        path=str(Path("C:/tmp/first.png").resolve()),
        label="[Image #1]",
    )
    assert message.content[2] == MessageLocalImageContentPart(
        path=str(Path("C:/tmp/second.png").resolve()),
        label="[Image #2]",
    )


def test_image_only_user_message_uses_stable_label_projection() -> None:
    message = Message.user(
        [
            MessageLocalImageContentPart(
                path=str(Path("C:/tmp/screenshot.png").resolve()),
                label="[Image #1]",
            )
        ]
    )

    assert message.text_content() == "[Image #1]"


def test_image_only_built_user_message_uses_stable_label_projection() -> None:
    message = build_user_message(
        "",
        image_paths=[Path("C:/tmp/screenshot.png")],
    )

    assert message.text_content() == "[Image #1]"


def test_standalone_prompt_image_paths_ignores_invalid_long_path() -> None:
    prompt = "a" * 5000

    updated_prompt, attachments = attach_standalone_prompt_image_paths(
        prompt,
        root=Path.cwd(),
    )

    assert updated_prompt == prompt
    assert attachments == []


def test_insert_attachment_reference_preserves_existing_text() -> None:
    buffer = Buffer(document=None)
    buffer.text = "before and after"
    buffer.cursor_position = len("before ")

    insert_attachment_reference(
        buffer,
        ImageAttachment(path=Path("C:/tmp/yoke-clipboard-ed6dv0bv.png")),
    )

    assert buffer.text == "before [yoke-clipboard-ed6dv0bv.png] and after"


def test_insert_attachment_reference_preserves_surrounding_text_with_multiple_images() -> (
    None
):
    buffer = Buffer(document=None)
    buffer.text = "[yoke-clipboard-ytwj90ce.png] and after gone; fix"
    buffer.cursor_position = len("[yoke-clipboard-ytwj90ce.png] and after ")

    insert_attachment_reference(
        buffer,
        ImageAttachment(path=Path("C:/tmp/yoke-clipboard-ed6dv0bv.png")),
    )

    assert (
        buffer.text
        == "[yoke-clipboard-ytwj90ce.png] and after [yoke-clipboard-ed6dv0bv.png] gone; fix"
    )
