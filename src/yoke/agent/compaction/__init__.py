"""Lazy public exports; generate the matching stub with docs/tools/generate_exports.py."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_LAZY_EXPORTS = {
    "COMPACTION_SUMMARY_PROMPT": "yoke.agent.compaction.types",
    "CompactionPolicy": "yoke.agent.compaction.types",
    "CompactionPreparation": "yoke.agent.compaction.types",
    "CompactionResult": "yoke.agent.compaction.types",
    "Compactor": "yoke.agent.compaction.core",
    "DEFAULT_HANDOFF_TARGET_TOKENS": "yoke.agent.compaction.types",
    "DEFAULT_IMAGE_DETAIL": "yoke.agent.compaction.types",
    "DEFAULT_KEEP_RECENT_TOKENS": "yoke.agent.compaction.types",
    "DEFAULT_OPENAI_MODEL_GROUP": "yoke.agent.compaction.types",
    "DEFAULT_RECENT_USER_TOKENS": "yoke.agent.compaction.types",
    "DEFAULT_RESERVED_OUTPUT_TOKENS": "yoke.agent.compaction.types",
    "DEFAULT_TOTAL_CONTEXT_TOKENS": "yoke.agent.compaction.types",
    "OPENAI_IMAGE_TOKEN_TABLE": "yoke.agent.compaction.types",
    "TOKEN_WIDTH_GUESS": "yoke.agent.compaction.types",
    "TokenEstimate": "yoke.agent.compaction.types",
    "CompactionBoundary": "yoke.agent.compaction.types",
    "CompactionReason": "yoke.agent.compaction.types",
    "build_compaction_summary_prompt": "yoke.agent.compaction.render",
    "is_real_user_message": "yoke.agent.compaction.render",
    "render_message": "yoke.agent.compaction.render",
    "summary_source_text": "yoke.agent.compaction.render",
    "truncate_message_to_token_budget": "yoke.agent.compaction.render",
    "truncate_structured_user_content": "yoke.agent.compaction.render",
    "ForcedCompaction": "yoke.agent.compaction.operations",
    "estimate_agent_context_usage": "yoke.agent.compaction.operations",
    "force_compact_agent": "yoke.agent.compaction.operations",
}

__all__ = tuple(_LAZY_EXPORTS)  # pyright: ignore[reportUnsupportedDunderAll]


def __getattr__(name: str) -> Any:
    """Resolve an export on first access, retaining normal module identity."""
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = (target, name) if isinstance(target, str) else target
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value
