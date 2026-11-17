"""Core context compaction utilities for summarizing message history."""

from __future__ import annotations

import base64
import io
import json
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image

from yoke.agent.compaction.render import (
    truncate_message_to_token_budget,
)
from yoke.agent.compaction.types import (
    COMPACTION_SUMMARY_PROMPT as COMPACTION_SUMMARY_PROMPT,
)
from yoke.agent.compaction.types import (
    DEFAULT_HANDOFF_TARGET_TOKENS as DEFAULT_HANDOFF_TARGET_TOKENS,
)
from yoke.agent.compaction.types import DEFAULT_IMAGE_DETAIL as DEFAULT_IMAGE_DETAIL
from yoke.agent.compaction.types import (
    DEFAULT_KEEP_RECENT_TOKENS as DEFAULT_KEEP_RECENT_TOKENS,
)
from yoke.agent.compaction.types import (
    DEFAULT_OPENAI_MODEL_GROUP as DEFAULT_OPENAI_MODEL_GROUP,
)
from yoke.agent.compaction.types import (
    DEFAULT_RECENT_USER_TOKENS as DEFAULT_RECENT_USER_TOKENS,
)
from yoke.agent.compaction.types import (
    DEFAULT_RESERVED_OUTPUT_TOKENS as DEFAULT_RESERVED_OUTPUT_TOKENS,
)
from yoke.agent.compaction.types import (
    DEFAULT_TOTAL_CONTEXT_TOKENS as DEFAULT_TOTAL_CONTEXT_TOKENS,
)
from yoke.agent.compaction.types import (
    OPENAI_IMAGE_TOKEN_TABLE as OPENAI_IMAGE_TOKEN_TABLE,
)
from yoke.agent.compaction.types import TOKEN_WIDTH_GUESS as TOKEN_WIDTH_GUESS
from yoke.agent.compaction.types import CompactionPolicy as CompactionPolicy
from yoke.agent.compaction.types import CompactionPreparation as CompactionPreparation
from yoke.agent.compaction.types import CompactionResult as CompactionResult
from yoke.agent.compaction.types import TokenEstimate as TokenEstimate
from yoke.agent.models import (
    ConversationEntry,
    Message,
    MessageImageURLContentPart,
    MessageLocalImageContentPart,
    MessageTextContentPart,
)


class Compactor:
    """Handles token estimation, decisions, and message compaction."""

    def __init__(
        self,
        *,
        model: str | None = None,
        provider_name: str | None = None,
    ) -> None:
        self.model = model or ""
        self.provider_name = provider_name or ""

    def estimate_tokens(
        self, messages: Sequence[Message], reserve_tokens: int
    ) -> TokenEstimate:
        """Estimate token counts for the given messages."""
        input_tokens = self._estimate_token_count(list(messages))
        return TokenEstimate(
            input_tokens=input_tokens,
            total_with_reserve=input_tokens + reserve_tokens,
        )

    def should_compact(
        self,
        estimate: TokenEstimate,
        *,
        policy: CompactionPolicy,
    ) -> bool:
        """Return True if compaction should be triggered for the estimate."""
        if not policy.enabled:
            return False
        input_budget = max(
            0,
            policy.max_total_tokens - policy.reserved_output_tokens,
        )
        if estimate.input_tokens > input_budget:
            return True
        if policy.soft_trigger_ratio is not None:
            soft_limit = int(input_budget * policy.soft_trigger_ratio)
            if estimate.input_tokens >= soft_limit:
                return True
        return False

    def collect_recent_user_messages(
        self,
        entries: Sequence[ConversationEntry],
        *,
        token_budget: int,
    ) -> list[Message]:
        """Collect recent user messages that fit within the token budget."""
        selected: list[Message] = []
        used_tokens = 0
        for entry in reversed(entries):
            if entry.kind != "user" or entry.message is None:
                continue
            message = entry.message
            message_tokens = self.estimate_tokens(
                [message], reserve_tokens=0
            ).input_tokens
            remaining = token_budget - used_tokens
            if message_tokens <= remaining:
                selected.insert(0, message.model_copy(deep=True))
                used_tokens += message_tokens
                continue
            if remaining <= 0:
                break
            truncated = truncate_message_to_token_budget(
                message, token_budget=remaining
            )
            if (
                truncated is not None
                and self.estimate_tokens([truncated], reserve_tokens=0).input_tokens
                <= remaining
            ):
                selected.insert(0, truncated)
            break
        return selected

    def _estimate_token_count(self, messages: list[Message]) -> int:
        total = 0
        for message in messages:
            total += self._estimate_message_tokens(message)
        return max(1, total)

    def _estimate_message_tokens(self, message: Message) -> int:
        role_tokens = self._estimate_text_tokens(message.role)
        content = message.content
        if content is None:
            return role_tokens
        if isinstance(content, str):
            return role_tokens + self._estimate_text_tokens(content)
        total = role_tokens
        for part in content:
            if isinstance(part, MessageTextContentPart):
                total += self._estimate_text_tokens(part.text)
                continue
            if isinstance(part, MessageLocalImageContentPart):
                total += self._estimate_local_image_tokens(part)
                continue
            if isinstance(part, MessageImageURLContentPart):
                total += self._estimate_remote_image_tokens(part)
        if message.tool_calls:
            total += self._estimate_text_tokens(
                json.dumps(
                    [tool_call.model_dump() for tool_call in message.tool_calls],
                    ensure_ascii=False,
                )
            )
        if message.tool_call_id:
            total += self._estimate_text_tokens(message.tool_call_id)
        return total

    def _estimate_text_tokens(self, text: str) -> int:
        if not text:
            return 0
        return max(1, (len(text) + TOKEN_WIDTH_GUESS - 1) // TOKEN_WIDTH_GUESS)

    def _estimate_local_image_tokens(self, part: MessageLocalImageContentPart) -> int:
        dimensions = _read_local_image_dimensions(part.path)
        detail = _normalize_image_detail(part.detail)
        if dimensions is None:
            return self._estimate_text_tokens(part.path)
        width, height = dimensions
        return self._estimate_image_tokens(
            width=width,
            height=height,
            detail=detail,
        )

    def _estimate_remote_image_tokens(self, part: MessageImageURLContentPart) -> int:
        detail = _normalize_image_detail(part.detail)
        dimensions = _read_data_url_image_dimensions(part.image_url.url)
        if dimensions is None:
            return _unknown_vision_estimate(detail=detail)
        width, height = dimensions
        return self._estimate_image_tokens(
            width=width,
            height=height,
            detail=detail,
        )

    def _estimate_image_tokens(
        self,
        *,
        width: int,
        height: int,
        detail: str,
    ) -> int:
        if detail == "low":
            return _vision_constants_for_model(self.model)["base"]
        if width <= 0 or height <= 0:
            return _unknown_vision_estimate(detail=detail)
        scaled_width, scaled_height = _scale_image_for_tiling(width, height)
        scaled_width, scaled_height = _normalize_tiling_short_side(
            scaled_width,
            scaled_height,
        )
        tiles_wide = max(1, (scaled_width + 511) // 512)
        tiles_high = max(1, (scaled_height + 511) // 512)
        tile_count = tiles_wide * tiles_high
        constants = _vision_constants_for_model(self.model)
        return constants["base"] + tile_count * constants["tile"]


def _scale_image_for_tiling(width: int, height: int) -> tuple[int, int]:
    max_side = max(width, height)
    if max_side <= 2048:
        return width, height
    scale = 2048 / max_side
    return max(1, int(width * scale)), max(1, int(height * scale))


def _normalize_tiling_short_side(
    width: int,
    height: int,
) -> tuple[int, int]:
    min_side = min(width, height)
    if min_side <= 768:
        return width, height
    scale = 768 / min_side
    return max(1, int(width * scale)), max(1, int(height * scale))


def _vision_constants_for_model(model: str | None) -> dict[str, int]:
    model_key = _model_group_for_vision(model)
    return OPENAI_IMAGE_TOKEN_TABLE.get(
        model_key,
        OPENAI_IMAGE_TOKEN_TABLE[DEFAULT_OPENAI_MODEL_GROUP],
    )


def _model_group_for_vision(model: str | None) -> str:
    if not model:
        return DEFAULT_OPENAI_MODEL_GROUP
    normalized = model.lower()
    if normalized.startswith("gpt-5"):
        return "gpt5"
    if normalized.startswith("gpt-4o-mini"):
        return "gpt4o_mini"
    if normalized.startswith("o1") or normalized.startswith("o3"):
        return "o1_o3"
    if "computer" in normalized:
        return "computer_use"
    if normalized.startswith("gpt-4.1") or normalized.startswith("gpt-4o"):
        return "gpt4o_4_1_4_5"
    return DEFAULT_OPENAI_MODEL_GROUP


def _unknown_vision_estimate(*, detail: str) -> int:
    constants = OPENAI_IMAGE_TOKEN_TABLE[DEFAULT_OPENAI_MODEL_GROUP]
    if detail == "low":
        return constants["base"]
    return constants["base"] + 5 * constants["tile"] + 89


def _normalize_image_detail(detail: str | None) -> str:
    normalized = (detail or DEFAULT_IMAGE_DETAIL).lower()
    if normalized not in {"low", "high", "auto"}:
        return DEFAULT_IMAGE_DETAIL
    if normalized == "auto":
        return DEFAULT_IMAGE_DETAIL
    return normalized


def _read_local_image_dimensions(path: str) -> tuple[int, int] | None:
    image_path = Path(path)
    if not image_path.exists() or not image_path.is_file():
        return None
    try:
        with Image.open(image_path) as image:
            return image.size
    except Exception:
        return None


def _read_data_url_image_dimensions(url: str) -> tuple[int, int] | None:
    parsed = urlparse(url)
    if parsed.scheme != "data":
        return None
    _, _, payload = url.partition(",")
    if not payload:
        return None
    try:
        data = base64.b64decode(payload)
        with Image.open(io.BytesIO(data)) as image:
            return image.size
    except Exception:
        return None
