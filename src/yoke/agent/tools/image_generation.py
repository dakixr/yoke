"""Codex-backed image generation tool."""

from __future__ import annotations

import base64
import os
from pathlib import Path
import tempfile
from typing import cast

from pydantic import Field

from yoke.agent.models import Message
from yoke.agent.models import MessageImageURLContentPart
from yoke.agent.models import MessageLocalImageContentPart
from yoke.agent.multimodal import build_image_user_message
from yoke.agent.multimodal import encode_local_image_data_url
from yoke.agent.multimodal import format_image_label
from yoke.agent.multimodal import next_image_label_index
from yoke.agent.multimodal import resolve_image_path
from yoke.agent.tools.base import LocalTool
from yoke.ai.providers.base import ProviderError

MAX_REFERENCE_IMAGES = 5


class ImageGenerationTool(LocalTool):
    """Generate an image with Codex subscription auth and attach it to context."""

    name = "image_generation"
    description = (
        "Generate an image using the active Codex provider. Provide clear "
        "generation prompt and an output_path where the PNG should be "
        "saved. The resulting image is attached to the conversation context."
    )
    execute_in_process = True

    prompt: str = Field(
        description="Detailed prompt for the image to generate or edit."
    )
    output_path: str = Field(
        description="Workspace-relative or absolute path where the generated PNG is saved."
    )
    referenced_image_paths: list[str] | None = Field(
        default=None,
        description=(
            "Optional local image paths to use as edit/reference inputs. "
            "Provide at most 5."
        ),
        max_length=MAX_REFERENCE_IMAGES,
    )
    num_last_images_to_include: int | None = Field(
        default=None,
        description=(
            "Optional number of most recent conversation images to use as "
            "edit/reference inputs. Use only when referenced_image_paths is omitted."
        ),
        ge=1,
        le=MAX_REFERENCE_IMAGES,
    )

    def execute(self) -> dict[str, object]:
        """Generate the image, write it to disk, and report the context label."""
        if self._is_cancel_requested():
            return {"ok": False, "cancelled": True}
        provider = self.context.provider
        generate_image = getattr(provider, "generate_image", None)
        edit_image = getattr(provider, "edit_image", None)
        if not callable(generate_image):
            return {
                "ok": False,
                "error": "The active provider does not support image generation.",
            }
        output_path = self._resolve_output_path(self.output_path)
        try:
            reference_image_urls = self._reference_image_urls()
            if reference_image_urls:
                if not callable(edit_image):
                    return {
                        "ok": False,
                        "error": "The active provider does not support referenced image generation.",
                    }
                encoded = edit_image(
                    prompt=self.prompt,
                    image_urls=reference_image_urls,
                )
            else:
                encoded = generate_image(prompt=self.prompt)
            if not isinstance(encoded, str):
                raise ProviderError("Image provider returned non-string image data.")
            image_bytes = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if self._is_cancel_requested():
            return {"ok": False, "cancelled": True}
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=output_path.parent,
                prefix=f".{output_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(image_bytes)
                temporary_path = Path(temporary.name)
            if self._is_cancel_requested():
                return {"ok": False, "cancelled": True}
            os.replace(temporary_path, output_path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        next_index = next_image_label_index(self._context_messages())
        return {
            "ok": True,
            "path": str(output_path),
            "label": format_image_label(next_index),
            "prompt": self.prompt,
            "reference_image_count": len(reference_image_urls),
            "bytes": len(image_bytes),
        }

    def apply_result(self, context, result: dict[str, object]) -> None:
        """Leave attachment construction to ordered context-message handling."""
        del context, result

    def pending_context_messages(self, result: dict[str, object]) -> list[Message]:
        """Build the generated-image message from the latest ordered context."""
        if not result.get("ok"):
            return []
        raw_path = result.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            return []
        next_index = next_image_label_index(self._context_messages())
        raw_prompt = result.get("prompt")
        prompt = raw_prompt if isinstance(raw_prompt, str) else ""
        message = build_image_user_message(
            f"Generated image for: {prompt}" if prompt else "Generated image",
            image_paths=[Path(raw_path)],
            start_index=next_index,
        )
        result["label"] = format_image_label(next_index)
        result["context_messages"] = [message.model_dump(mode="json")]
        return [message]

    def _resolve_output_path(self, raw_path_value: str) -> Path:
        if not raw_path_value.strip():
            raise ValueError("output_path must be a non-empty path")
        raw_path = Path(raw_path_value)
        root = self.context.root
        candidate = raw_path if raw_path.is_absolute() else root / raw_path
        return candidate.resolve()

    def _reference_image_urls(self) -> list[str]:
        explicit_paths = self.referenced_image_paths or []
        if explicit_paths and self.num_last_images_to_include is not None:
            raise ValueError(
                "provide only one of referenced_image_paths or num_last_images_to_include"
            )
        if len(explicit_paths) > MAX_REFERENCE_IMAGES:
            raise ValueError(
                f"referenced_image_paths must contain at most {MAX_REFERENCE_IMAGES} paths"
            )
        if explicit_paths:
            return [
                encode_local_image_data_url(
                    resolve_image_path(path, root=self.context.root)
                )
                for path in explicit_paths
            ]
        if self.num_last_images_to_include is None:
            return []
        images = _recent_image_urls(
            self._context_messages(), self.num_last_images_to_include
        )
        if len(images) != self.num_last_images_to_include:
            raise ValueError(
                "requested the last "
                f"{self.num_last_images_to_include} conversation images, but only "
                f"{len(images)} were available"
            )
        return images

    def _context_messages(self) -> list[Message]:
        raw_messages = self._context.get("messages", [])
        if isinstance(raw_messages, list) and all(
            isinstance(message, Message) for message in raw_messages
        ):
            return [cast(Message, message) for message in raw_messages]
        return []


def provider_supports_image_generation(provider: object) -> bool:
    """Return whether a provider exposes Codex image generation support."""
    provider_name = getattr(provider, "provider_name", None)
    if provider_name != "codex":
        return False
    return bool(getattr(provider, "supports_image_generation", False)) and callable(
        getattr(provider, "generate_image", None)
    )


def _recent_image_urls(messages: list[Message], count: int) -> list[str]:
    images: list[str] = []
    for message in reversed(messages):
        content = message.content
        if not isinstance(content, list):
            continue
        for part in reversed(content):
            if isinstance(part, MessageLocalImageContentPart):
                images.append(part.data_url or encode_local_image_data_url(part.path))
            elif isinstance(part, MessageImageURLContentPart):
                images.append(part.image_url.url)
            if len(images) == count:
                return list(reversed(images))
    return list(reversed(images))
