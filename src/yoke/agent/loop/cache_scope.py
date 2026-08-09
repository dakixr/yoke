"""Prompt-cache scopes for model and compaction requests."""

from __future__ import annotations

from yoke.agent.models import AgentContext


def conversation_cache_scope(context: AgentContext) -> str:
    """Return the stable cache scope for one logical conversation."""
    return _base_conversation_cache_scope(context)


def _base_conversation_cache_scope(context: AgentContext) -> str:
    for entry in context.conversation_log.entries:
        if entry.kind not in {"instruction", "memory_snapshot"}:
            return entry.id
    return "empty-conversation"
