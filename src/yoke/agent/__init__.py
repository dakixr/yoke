"""Lazy public exports for the Yoke agent runtime."""

# ruff: noqa: F401

from __future__ import annotations

from importlib import import_module
from typing import Any
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from yoke.agent.budget import ProviderCompactionBudget
    from yoke.agent.budget import build_provider_context_manager
    from yoke.agent.budget import current_context_fits_provider_budget
    from yoke.agent.budget import rebind_context_manager_budget
    from yoke.agent.budget import resolve_provider_compaction_budget
    from yoke.agent.compaction import CompactionPolicy
    from yoke.agent.compaction import CompactionPreparation
    from yoke.agent.compaction import CompactionResult
    from yoke.agent.compaction import ForcedCompaction
    from yoke.agent.compaction import TokenEstimate
    from yoke.agent.compaction import estimate_agent_context_usage
    from yoke.agent.compaction import force_compact_agent
    from yoke.agent.context import ContextManager
    from yoke.agent.loop import AfterToolCallContext
    from yoke.agent.loop import AfterToolCallResult
    from yoke.agent.loop import AgentResult
    from yoke.agent.loop import BeforeToolCallContext
    from yoke.agent.loop import BeforeToolCallResult
    from yoke.agent.loop import RuntimeAgent
    from yoke.agent.models import AgentContext
    from yoke.agent.models import CompactionHandoff
    from yoke.agent.models import ConversationEntry
    from yoke.agent.models import ConversationLog
    from yoke.agent.models import MemorySnapshot
    from yoke.agent.persistence import AgentStateLoadError
    from yoke.agent.persistence import AgentStatePersistenceError
    from yoke.agent.persistence import AgentStateSaveError
    from yoke.agent.persistence import AgentStateSnapshot
    from yoke.agent.persistence import load_agent_state
    from yoke.agent.persistence import load_agent_state_snapshot
    from yoke.agent.persistence import restore_agent_state
    from yoke.agent.persistence import save_agent_state
    from yoke.agent.prompting import PromptContext
    from yoke.agent.protocols import AgentRunner
    from yoke.agent.state import AgentState
    from yoke.agent.state import capture_agent_state
    from yoke.agent.state import hydrate_agent_state

_LAZY_EXPORTS = {
    "CompactionPolicy": ("yoke.agent.compaction", "CompactionPolicy"),
    "CompactionPreparation": ("yoke.agent.compaction", "CompactionPreparation"),
    "CompactionResult": ("yoke.agent.compaction", "CompactionResult"),
    "ForcedCompaction": ("yoke.agent.compaction", "ForcedCompaction"),
    "TokenEstimate": ("yoke.agent.compaction", "TokenEstimate"),
    "estimate_agent_context_usage": (
        "yoke.agent.compaction",
        "estimate_agent_context_usage",
    ),
    "force_compact_agent": ("yoke.agent.compaction", "force_compact_agent"),
    "ContextManager": ("yoke.agent.context", "ContextManager"),
    "ProviderCompactionBudget": (
        "yoke.agent.budget",
        "ProviderCompactionBudget",
    ),
    "build_provider_context_manager": (
        "yoke.agent.budget",
        "build_provider_context_manager",
    ),
    "current_context_fits_provider_budget": (
        "yoke.agent.budget",
        "current_context_fits_provider_budget",
    ),
    "rebind_context_manager_budget": (
        "yoke.agent.budget",
        "rebind_context_manager_budget",
    ),
    "resolve_provider_compaction_budget": (
        "yoke.agent.budget",
        "resolve_provider_compaction_budget",
    ),
    "RuntimeAgent": ("yoke.agent.loop", "RuntimeAgent"),
    "AfterToolCallContext": ("yoke.agent.loop", "AfterToolCallContext"),
    "AfterToolCallResult": ("yoke.agent.loop", "AfterToolCallResult"),
    "AgentResult": ("yoke.agent.loop", "AgentResult"),
    "BeforeToolCallContext": ("yoke.agent.loop", "BeforeToolCallContext"),
    "BeforeToolCallResult": ("yoke.agent.loop", "BeforeToolCallResult"),
    "AgentContext": ("yoke.agent.models", "AgentContext"),
    "CompactionHandoff": ("yoke.agent.models", "CompactionHandoff"),
    "ConversationEntry": ("yoke.agent.models", "ConversationEntry"),
    "ConversationLog": ("yoke.agent.models", "ConversationLog"),
    "MemorySnapshot": ("yoke.agent.models", "MemorySnapshot"),
    "PromptContext": ("yoke.agent.prompting", "PromptContext"),
    "AgentRunner": ("yoke.agent.protocols", "AgentRunner"),
    "AgentStateLoadError": ("yoke.agent.persistence", "AgentStateLoadError"),
    "AgentStatePersistenceError": (
        "yoke.agent.persistence",
        "AgentStatePersistenceError",
    ),
    "AgentStateSaveError": ("yoke.agent.persistence", "AgentStateSaveError"),
    "AgentStateSnapshot": ("yoke.agent.persistence", "AgentStateSnapshot"),
    "load_agent_state": ("yoke.agent.persistence", "load_agent_state"),
    "load_agent_state_snapshot": (
        "yoke.agent.persistence",
        "load_agent_state_snapshot",
    ),
    "restore_agent_state": ("yoke.agent.persistence", "restore_agent_state"),
    "save_agent_state": ("yoke.agent.persistence", "save_agent_state"),
    "AgentState": ("yoke.agent.state", "AgentState"),
    "capture_agent_state": ("yoke.agent.state", "capture_agent_state"),
    "hydrate_agent_state": ("yoke.agent.state", "hydrate_agent_state"),
}

__all__ = (
    "CompactionPolicy",
    "CompactionPreparation",
    "CompactionResult",
    "ForcedCompaction",
    "TokenEstimate",
    "estimate_agent_context_usage",
    "force_compact_agent",
    "ContextManager",
    "ProviderCompactionBudget",
    "build_provider_context_manager",
    "current_context_fits_provider_budget",
    "rebind_context_manager_budget",
    "resolve_provider_compaction_budget",
    "RuntimeAgent",
    "AfterToolCallContext",
    "AfterToolCallResult",
    "AgentResult",
    "BeforeToolCallContext",
    "BeforeToolCallResult",
    "AgentContext",
    "CompactionHandoff",
    "ConversationEntry",
    "ConversationLog",
    "MemorySnapshot",
    "PromptContext",
    "AgentRunner",
    "AgentStateLoadError",
    "AgentStatePersistenceError",
    "AgentStateSaveError",
    "AgentStateSnapshot",
    "load_agent_state",
    "load_agent_state_snapshot",
    "restore_agent_state",
    "save_agent_state",
    "AgentState",
    "capture_agent_state",
    "hydrate_agent_state",
)


def __getattr__(name: str) -> Any:  # noqa: ANN401
    """Resolve public agent objects without eagerly importing the runtime."""
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value
