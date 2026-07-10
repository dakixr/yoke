"""Tool for attaching disk images into multimodal conversation context."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from pydantic import Field

from yoke.agent.multimodal import build_image_user_message
from yoke.agent.multimodal import format_image_label
from yoke.agent.multimodal import next_image_label_index
from yoke.agent.multimodal import resolve_image_path
from yoke.agent.models import Message
from yoke.agent.tools.base import LocalTool


class AttachImageTool(LocalTool):
    """Attach a local image into the next conversation context turn."""

    name = "attach_image"
    description = (
        "Attach an image from disk into the conversation context so the model "
        "can inspect it natively in a following turn. Use this when an image "
        "file exists locally and should become part of the multimodal context."
    )

    path: str = Field(description="Path to a local image file on disk")
    caption: str | None = Field(
        default=None,
        description=(
            "Optional text to include alongside the attached image so the "
            "model can reference why it was attached"
        ),
    )

    def execute(self) -> dict[str, object]:
        """Validate the path and report the label that will be assigned."""
        raw_root = self._context.get("root")
        root = Path(raw_root) if isinstance(raw_root, Path | str) else Path.cwd()
        resolved = resolve_image_path(self.path, root=root)
        next_index = next_image_label_index(self._context_messages())
        return {
            "ok": True,
            "path": str(resolved),
            "label": format_image_label(next_index),
            "caption": self.caption,
        }

    def apply_result(self, context, result: dict[str, object]) -> None:
        """Leave attachment construction to ordered context-message handling."""
        del context, result

    def pending_context_messages(self, result: dict[str, object]) -> list[Message]:
        """Build the attached message using the latest ordered context."""
        if not result.get("ok"):
            return []
        raw_path = result.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            return []
        next_index = next_image_label_index(self._context_messages())
        caption = result.get("caption")
        prompt = caption if isinstance(caption, str) else ""
        message = build_image_user_message(
            prompt,
            image_paths=[Path(raw_path)],
            start_index=next_index,
        )
        result["label"] = format_image_label(next_index)
        result["context_messages"] = [message.model_dump(mode="json")]
        return [message]

    def _context_messages(self) -> list[Message]:
        raw_messages = self._context.get("messages", [])
        if isinstance(raw_messages, list) and all(
            isinstance(message, Message) for message in raw_messages
        ):
            return [cast(Message, message) for message in raw_messages]
        return []
