"""Context compaction utilities for summarizing message history."""

from __future__ import annotations

from importlib import import_module
from typing import Any
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from yoke.agent.compaction.core import (
        COMPACTION_SUMMARY_PROMPT as COMPACTION_SUMMARY_PROMPT,
    )
    from yoke.agent.compaction.core import DEFAULT_IMAGE_DETAIL as DEFAULT_IMAGE_DETAIL
    from yoke.agent.compaction.core import (
        DEFAULT_KEEP_RECENT_TOKENS as DEFAULT_KEEP_RECENT_TOKENS,
    )
    from yoke.agent.compaction.core import (
        DEFAULT_OPENAI_MODEL_GROUP as DEFAULT_OPENAI_MODEL_GROUP,
    )
    from yoke.agent.compaction.core import (
        DEFAULT_RECENT_USER_TOKENS as DEFAULT_RECENT_USER_TOKENS,
    )
    from yoke.agent.compaction.core import (
        DEFAULT_RESERVED_OUTPUT_TOKENS as DEFAULT_RESERVED_OUTPUT_TOKENS,
    )
    from yoke.agent.compaction.core import (
        DEFAULT_TOTAL_CONTEXT_TOKENS as DEFAULT_TOTAL_CONTEXT_TOKENS,
    )
    from yoke.agent.compaction.core import (
        OPENAI_IMAGE_TOKEN_TABLE as OPENAI_IMAGE_TOKEN_TABLE,
    )
    from yoke.agent.compaction.core import TOKEN_WIDTH_GUESS as TOKEN_WIDTH_GUESS
    from yoke.agent.compaction.core import CompactionPolicy as CompactionPolicy
    from yoke.agent.compaction.core import (
        CompactionPreparation as CompactionPreparation,
    )
    from yoke.agent.compaction.core import CompactionResult as CompactionResult
    from yoke.agent.compaction.core import Compactor as Compactor
    from yoke.agent.compaction.core import TokenEstimate as TokenEstimate
    from yoke.agent.compaction.operations import ForcedCompaction as ForcedCompaction
    from yoke.agent.compaction.operations import (
        estimate_agent_context_usage as estimate_agent_context_usage,
    )
    from yoke.agent.compaction.operations import (
        force_compact_agent as force_compact_agent,
    )
    from yoke.agent.compaction.render import (
        build_summary_handoff_messages as build_summary_handoff_messages,
    )
    from yoke.agent.compaction.render import (
        is_real_user_message as is_real_user_message,
    )
    from yoke.agent.compaction.render import render_message as render_message
    from yoke.agent.compaction.render import summary_source_text as summary_source_text
    from yoke.agent.compaction.render import (
        truncate_message_to_token_budget as truncate_message_to_token_budget,
    )
    from yoke.agent.compaction.render import (
        truncate_structured_user_content as truncate_structured_user_content,
    )
    from yoke.agent.compaction.types import CompactionBoundary as CompactionBoundary
    from yoke.agent.compaction.types import CompactionReason as CompactionReason

_LAZY_EXPORTS = {
    "COMPACTION_SUMMARY_PROMPT": (
        "yoke.agent.compaction.core",
        "COMPACTION_SUMMARY_PROMPT",
    ),
    "CompactionPolicy": ("yoke.agent.compaction.core", "CompactionPolicy"),
    "CompactionPreparation": (
        "yoke.agent.compaction.core",
        "CompactionPreparation",
    ),
    "CompactionResult": ("yoke.agent.compaction.core", "CompactionResult"),
    "Compactor": ("yoke.agent.compaction.core", "Compactor"),
    "DEFAULT_IMAGE_DETAIL": ("yoke.agent.compaction.core", "DEFAULT_IMAGE_DETAIL"),
    "DEFAULT_KEEP_RECENT_TOKENS": (
        "yoke.agent.compaction.core",
        "DEFAULT_KEEP_RECENT_TOKENS",
    ),
    "DEFAULT_TOTAL_CONTEXT_TOKENS": (
        "yoke.agent.compaction.core",
        "DEFAULT_TOTAL_CONTEXT_TOKENS",
    ),
    "DEFAULT_OPENAI_MODEL_GROUP": (
        "yoke.agent.compaction.core",
        "DEFAULT_OPENAI_MODEL_GROUP",
    ),
    "DEFAULT_RESERVED_OUTPUT_TOKENS": (
        "yoke.agent.compaction.core",
        "DEFAULT_RESERVED_OUTPUT_TOKENS",
    ),
    "DEFAULT_RECENT_USER_TOKENS": (
        "yoke.agent.compaction.core",
        "DEFAULT_RECENT_USER_TOKENS",
    ),
    "OPENAI_IMAGE_TOKEN_TABLE": (
        "yoke.agent.compaction.core",
        "OPENAI_IMAGE_TOKEN_TABLE",
    ),
    "TOKEN_WIDTH_GUESS": ("yoke.agent.compaction.core", "TOKEN_WIDTH_GUESS"),
    "TokenEstimate": ("yoke.agent.compaction.core", "TokenEstimate"),
    "CompactionBoundary": ("yoke.agent.compaction.types", "CompactionBoundary"),
    "CompactionReason": ("yoke.agent.compaction.types", "CompactionReason"),
    "build_summary_handoff_messages": (
        "yoke.agent.compaction.render",
        "build_summary_handoff_messages",
    ),
    "is_real_user_message": ("yoke.agent.compaction.render", "is_real_user_message"),
    "render_message": ("yoke.agent.compaction.render", "render_message"),
    "summary_source_text": ("yoke.agent.compaction.render", "summary_source_text"),
    "truncate_message_to_token_budget": (
        "yoke.agent.compaction.render",
        "truncate_message_to_token_budget",
    ),
    "truncate_structured_user_content": (
        "yoke.agent.compaction.render",
        "truncate_structured_user_content",
    ),
    "ForcedCompaction": ("yoke.agent.compaction.operations", "ForcedCompaction"),
    "estimate_agent_context_usage": (
        "yoke.agent.compaction.operations",
        "estimate_agent_context_usage",
    ),
    "force_compact_agent": ("yoke.agent.compaction.operations", "force_compact_agent"),
}

__all__ = [
    "COMPACTION_SUMMARY_PROMPT",
    "CompactionPolicy",
    "CompactionPreparation",
    "CompactionResult",
    "Compactor",
    "DEFAULT_IMAGE_DETAIL",
    "DEFAULT_KEEP_RECENT_TOKENS",
    "DEFAULT_TOTAL_CONTEXT_TOKENS",
    "DEFAULT_OPENAI_MODEL_GROUP",
    "DEFAULT_RESERVED_OUTPUT_TOKENS",
    "DEFAULT_RECENT_USER_TOKENS",
    "OPENAI_IMAGE_TOKEN_TABLE",
    "TOKEN_WIDTH_GUESS",
    "TokenEstimate",
    "CompactionBoundary",
    "CompactionReason",
    "build_summary_handoff_messages",
    "is_real_user_message",
    "render_message",
    "summary_source_text",
    "truncate_message_to_token_budget",
    "truncate_structured_user_content",
    "ForcedCompaction",
    "estimate_agent_context_usage",
    "force_compact_agent",
]


def __getattr__(name: str) -> Any:
    """Lazily resolve compaction package re-exports."""
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value
