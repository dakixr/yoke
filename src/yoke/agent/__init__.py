"""Lazy public exports; generate the matching stub with docs/tools/generate_exports.py."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_LAZY_EXPORTS = {
    "CompactionPolicy": "yoke.agent.compaction",
    "CompactionPreparation": "yoke.agent.compaction",
    "CompactionResult": "yoke.agent.compaction",
    "ForcedCompaction": "yoke.agent.compaction",
    "TokenEstimate": "yoke.agent.compaction",
    "estimate_agent_context_usage": "yoke.agent.compaction",
    "force_compact_agent": "yoke.agent.compaction",
    "ContextManager": "yoke.agent.context",
    "ProviderCompactionBudget": "yoke.agent.budget",
    "build_provider_context_manager": "yoke.agent.budget",
    "current_context_fits_provider_budget": "yoke.agent.budget",
    "rebind_context_manager_budget": "yoke.agent.budget",
    "resolve_provider_compaction_budget": "yoke.agent.budget",
    "RuntimeAgent": "yoke.agent.loop",
    "AfterToolCallContext": "yoke.agent.loop",
    "AfterToolCallResult": "yoke.agent.loop",
    "AgentResult": "yoke.agent.loop",
    "BeforeToolCallContext": "yoke.agent.loop",
    "BeforeToolCallResult": "yoke.agent.loop",
    "AgentContext": "yoke.agent.models",
    "CompactionHandoff": "yoke.agent.models",
    "ConversationEntry": "yoke.agent.models",
    "ConversationLog": "yoke.agent.models",
    "MemorySnapshot": "yoke.agent.models",
    "PromptContext": "yoke.agent.prompting",
    "AgentStateLoadError": "yoke.agent.persistence",
    "AgentStatePersistenceError": "yoke.agent.persistence",
    "AgentStateSaveError": "yoke.agent.persistence",
    "AgentStateSnapshot": "yoke.agent.persistence",
    "load_agent_state": "yoke.agent.persistence",
    "load_agent_state_snapshot": "yoke.agent.persistence",
    "restore_agent_state": "yoke.agent.persistence",
    "save_agent_state": "yoke.agent.persistence",
    "AgentState": "yoke.agent.state",
    "capture_agent_state": "yoke.agent.state",
    "hydrate_agent_state": "yoke.agent.state",
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
