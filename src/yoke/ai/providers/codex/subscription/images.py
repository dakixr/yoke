"""Codex subscription provider implementation."""

# ruff: noqa: ANN401,C901,D101,D102,D103,E501,S105

from __future__ import annotations

from typing import Any


from yoke.ai.providers.base import (
    ProviderError,
)

from .helpers import clamp_reasoning_effort, error_detail
from .sse import consume_hosted_image_sse_response


class CodexImageMixin:
    def generate_image(self: Any, *, prompt: str) -> str:
        """Generate an image through Codex's hosted Responses image tool."""
        return self._generate_hosted_image(
            prompt=prompt,
            reference_image_urls=[],
        )

    def edit_image(self: Any, *, prompt: str, image_urls: list[str]) -> str:
        """Generate an edited image using reference image data URLs."""
        if not image_urls:
            raise ProviderError(
                "Codex image edit requires at least one reference image."
            )
        return self._generate_hosted_image(
            prompt=prompt,
            reference_image_urls=image_urls,
        )

    def _generate_hosted_image(
        self: Any, *, prompt: str, reference_image_urls: list[str]
    ) -> str:
        credentials = self._fresh_credentials()
        payload = self._hosted_image_payload(
            prompt=prompt,
            reference_image_urls=reference_image_urls,
        )
        with self._client.stream(
            "POST",
            self._responses_url(),
            json=payload,
            headers=self._request_headers(credentials),
        ) as response:
            if response.is_error:
                operation = "edit" if reference_image_urls else "generation"
                raise ProviderError(
                    f"Codex image {operation} failed: {error_detail(response)}",
                    status_code=response.status_code,
                )
            return consume_hosted_image_sse_response(response)

    def _hosted_image_payload(
        self: Any, *, prompt: str, reference_image_urls: list[str]
    ) -> dict[str, object]:
        content: list[dict[str, object]] = [
            {"type": "input_text", "text": prompt},
        ]
        content.extend(
            {"type": "input_image", "image_url": image_url}
            for image_url in reference_image_urls
        )
        return {
            "model": self.config.model,
            "store": False,
            "stream": True,
            "instructions": (
                "Use the hosted image_generation tool to generate exactly one PNG "
                "image for the user's prompt."
            ),
            "input": [{"role": "user", "content": content}],
            "tools": [{"type": "image_generation", "output_format": "png"}],
            "tool_choice": "auto",
            "parallel_tool_calls": True,
            "reasoning": {
                "effort": clamp_reasoning_effort(
                    self.config.model, self.config.reasoning_effort
                ),
                "summary": "auto",
            },
            "text": {"verbosity": self.config.text_verbosity},
            "include": ["reasoning.encrypted_content"],
            "prompt_cache_key": self._prompt_cache_key,
        }
