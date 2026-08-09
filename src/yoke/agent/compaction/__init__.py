"""Lazy exports for context compaction."""

# ruff: noqa: F401

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .core import (
        COMPACTION_SUMMARY_PROMPT,
        DEFAULT_HANDOFF_TARGET_TOKENS,
        DEFAULT_IMAGE_DETAIL,
        DEFAULT_KEEP_RECENT_TOKENS,
        DEFAULT_OPENAI_MODEL_GROUP,
        DEFAULT_RECENT_USER_TOKENS,
        DEFAULT_RESERVED_OUTPUT_TOKENS,
        DEFAULT_TOTAL_CONTEXT_TOKENS,
        OPENAI_IMAGE_TOKEN_TABLE,
        TOKEN_WIDTH_GUESS,
        CompactionPolicy,
        CompactionPreparation,
        CompactionResult,
        Compactor,
        TokenEstimate,
    )
    from .operations import (
        ForcedCompaction,
        estimate_agent_context_usage,
        force_compact_agent,
    )
    from .render import (
        build_compaction_summary_prompt,
        build_summary_handoff_messages,
        is_real_user_message,
        render_message,
        summary_source_text,
        truncate_message_to_token_budget,
        truncate_structured_user_content,
    )
    from .types import CompactionBoundary, CompactionReason

_CORE_EXPORTS = (
    "COMPACTION_SUMMARY_PROMPT",
    "CompactionPolicy",
    "CompactionPreparation",
    "CompactionResult",
    "Compactor",
    "DEFAULT_HANDOFF_TARGET_TOKENS",
    "DEFAULT_IMAGE_DETAIL",
    "DEFAULT_KEEP_RECENT_TOKENS",
    "DEFAULT_OPENAI_MODEL_GROUP",
    "DEFAULT_RECENT_USER_TOKENS",
    "DEFAULT_RESERVED_OUTPUT_TOKENS",
    "DEFAULT_TOTAL_CONTEXT_TOKENS",
    "OPENAI_IMAGE_TOKEN_TABLE",
    "TOKEN_WIDTH_GUESS",
    "TokenEstimate",
)
_TYPE_EXPORTS = ("CompactionBoundary", "CompactionReason")
_RENDER_EXPORTS = (
    "build_compaction_summary_prompt",
    "build_summary_handoff_messages",
    "is_real_user_message",
    "render_message",
    "summary_source_text",
    "truncate_message_to_token_budget",
    "truncate_structured_user_content",
)
_OPERATION_EXPORTS = (
    "ForcedCompaction",
    "estimate_agent_context_usage",
    "force_compact_agent",
)

_LAZY_EXPORTS = {
    **{name: ("yoke.agent.compaction.core", name) for name in _CORE_EXPORTS},
    **{name: ("yoke.agent.compaction.types", name) for name in _TYPE_EXPORTS},
    **{name: ("yoke.agent.compaction.render", name) for name in _RENDER_EXPORTS},
    **{name: ("yoke.agent.compaction.operations", name) for name in _OPERATION_EXPORTS},
}

__all__ = (
    "COMPACTION_SUMMARY_PROMPT",
    "CompactionPolicy",
    "CompactionPreparation",
    "CompactionResult",
    "Compactor",
    "DEFAULT_HANDOFF_TARGET_TOKENS",
    "DEFAULT_IMAGE_DETAIL",
    "DEFAULT_KEEP_RECENT_TOKENS",
    "DEFAULT_OPENAI_MODEL_GROUP",
    "DEFAULT_RECENT_USER_TOKENS",
    "DEFAULT_RESERVED_OUTPUT_TOKENS",
    "DEFAULT_TOTAL_CONTEXT_TOKENS",
    "OPENAI_IMAGE_TOKEN_TABLE",
    "TOKEN_WIDTH_GUESS",
    "TokenEstimate",
    "CompactionBoundary",
    "CompactionReason",
    "build_compaction_summary_prompt",
    "build_summary_handoff_messages",
    "is_real_user_message",
    "render_message",
    "summary_source_text",
    "truncate_message_to_token_budget",
    "truncate_structured_user_content",
    "ForcedCompaction",
    "estimate_agent_context_usage",
    "force_compact_agent",
)


def __getattr__(name: str) -> Any:  # noqa: ANN401
    """Resolve a compaction export without importing unrelated runtime code."""
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value
