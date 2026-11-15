"""Leaf values, constants and policies shared by compaction and message models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from yoke.agent.models import Message

CompactionBoundary = Literal["user", "assistant", "split_turn"]
CompactionReason = Literal["threshold", "overflow_retry", "forced", "manual"]


TOKEN_WIDTH_GUESS = 4
DEFAULT_TOTAL_CONTEXT_TOKENS = 400_000
DEFAULT_KEEP_RECENT_TOKENS = 20_000
DEFAULT_RECENT_USER_TOKENS = 20_000
DEFAULT_HANDOFF_TARGET_TOKENS = 12_000
DEFAULT_RESERVED_OUTPUT_TOKENS = 128_000
DEFAULT_OPENAI_MODEL_GROUP = "gpt4o_4_1_4_5"
DEFAULT_IMAGE_DETAIL = "high"
OPENAI_IMAGE_TOKEN_TABLE = {
    "gpt5": {"base": 70, "tile": 140},
    "gpt4o_4_1_4_5": {"base": 85, "tile": 170},
    "gpt4o_mini": {"base": 2833, "tile": 5667},
    "o1_o3": {"base": 75, "tile": 150},
    "computer_use": {"base": 65, "tile": 129},
}
COMPACTION_SUMMARY_PROMPT = "\n".join(
    [
        "You are performing a CONTEXT CHECKPOINT COMPACTION. Create a "
        "handoff summary for another LLM that will resume the task.",
        "",
        "Include:",
        "- Current progress and key decisions made",
        "- Important context, constraints, or user preferences",
        "- What remains to be done (clear next steps)",
        "- Any critical data, examples, or references needed to continue",
        "",
        "Rules:",
        "- Be concise but concrete",
        "- Preserve important constraints exactly",
        "- Preserve file names, commands, APIs, config values, and decisions "
        "when relevant",
        "- Do not restate obvious boilerplate",
        "- Do not include raw logs unless essential",
        "- Do not call tools",
        "- Return only the handoff summary as response text",
        "- Keep the handoff below 12,000 estimated tokens",
        "- Assume the next model will not see prior assistant replies or tool output",
        "- Optimize for seamless continuation by another agent",
    ]
)


@dataclass(slots=True, frozen=True)
class CompactionPolicy:
    """Policy settings controlling when and how compaction is applied."""

    max_total_tokens: int = DEFAULT_TOTAL_CONTEXT_TOKENS
    reserved_output_tokens: int = DEFAULT_RESERVED_OUTPUT_TOKENS
    keep_recent_tokens: int = DEFAULT_KEEP_RECENT_TOKENS
    recent_user_tokens: int = DEFAULT_RECENT_USER_TOKENS
    handoff_target_tokens: int = DEFAULT_HANDOFF_TARGET_TOKENS
    soft_trigger_ratio: float | None = 0.95
    enabled: bool = True


@dataclass(slots=True, frozen=True)
class TokenEstimate:
    """Estimated token counts for a sequence of messages."""

    input_tokens: int
    total_with_reserve: int


@dataclass(slots=True, frozen=True)
class CompactionPreparation:
    """Data gathered before performing a context compaction."""

    reason: CompactionReason
    estimate: TokenEstimate
    boundary: CompactionBoundary
    messages_to_summarize: list[Message]
    kept_messages: list[Message]
    recent_user_messages: list[Message] = field(default_factory=list)
    turn_prefix_messages: list[Message] = field(default_factory=list)


@dataclass(slots=True, frozen=True)
class CompactionResult:
    """Result of a context compaction with new messages and summary."""

    messages: list[Message]
    summary_text: str
