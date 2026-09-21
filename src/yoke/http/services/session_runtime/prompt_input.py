"""Native attachment projection and promptless input validation."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from yoke.agent.models import (
    Message,
    MessageContentPart,
    MessageTextContentPart,
    MessageImageURLContentPart,
)
from yoke.agent.multimodal import build_image_user_message, next_image_label_index
from yoke.http.errors import ApiError
from yoke.http.models.prompt import PromptInput
from yoke.session.admissions import AdmissionRecord

if TYPE_CHECKING:
    from yoke.http.services.pending_input_service import PendingInputService
    from yoke.session import SessionRecord


def validate_prompt(
    service: PendingInputService, session_id: str, prompt: PromptInput
) -> None:
    if prompt.continuation:
        if prompt.text or prompt.attachments:
            raise ApiError(
                400, "invalid_continuation", "Continuation must not contain user input."
            )
        record = service.store.load(session_id)
        if record is None or not any(
            message.role == "user" for message in record.messages
        ):
            raise ApiError(
                400, "empty_continuation", "Continuation requires conversation history."
            )
        return
    if len(prompt.attachments) > 20:
        raise ApiError(
            400, "too_many_attachments", "Prompt accepts at most 20 attachments."
        )
    for attachment in prompt.attachments:
        service.uploads.validate_reference(
            attachment.uri,
            session_id=session_id,
            name=attachment.name,
            mime=attachment.mime,
        )
    if not prompt.text.strip() and not prompt.attachments:
        raise ApiError(400, "empty_prompt", "Prompt text cannot be empty.")
    if len(prompt.text.encode()) > 1_048_576:
        raise ApiError(413, "prompt_too_large", "Prompt exceeds the server limit.")


def user_message(
    service: PendingInputService,
    session_id: str,
    record: SessionRecord,
    admission: AdmissionRecord,
) -> Message:
    images = []
    files = []
    for attachment in admission.attachments:
        path = service.uploads.resolve(
            attachment.uri,
            session_id=session_id,
            name=attachment.name,
            mime=attachment.mime,
        )
        if attachment.mime.startswith("image/"):
            images.append(path)
        else:
            files.append(
                {"name": attachment.name, "mime": attachment.mime, "path": str(path)}
            )
    message = build_image_user_message(
        admission.prompt,
        image_paths=images,
        start_index=next_image_label_index(record.messages),
        embed_local_images=True,
    )
    if isinstance(message.content, list):
        image_parts = [
            part
            for part in message.content
            if isinstance(part, MessageImageURLContentPart)
        ]
        for part, path in zip(image_parts, images, strict=True):
            part.attachment_name = path.name
    if files:
        # File contents are deliberately not injected. Native read/extract tools
        # can open these daemon-owned paths when relevant to the task.
        references = MessageTextContentPart(
            text="Attached files (use file-reading tools):\n"
            + json.dumps(files, ensure_ascii=False)
        )
        if isinstance(message.content, list):
            message.content.append(references)
        else:
            content: list[MessageContentPart] = (
                [MessageTextContentPart(text=admission.prompt)]
                if admission.prompt
                else []
            )
            content.append(references)
            message.content = content
    return message


def validate_execution_images(admission: AdmissionRecord, provider: object) -> None:
    """Reject newly attached images if selection changed after ACP validation."""
    from yoke.agent.multimodal import provider_supports_image_inputs

    if (
        any(item.mime.startswith("image/") for item in admission.attachments)
        and provider_supports_image_inputs(provider) is False
    ):
        raise ValueError("Selected model does not support image inputs")
