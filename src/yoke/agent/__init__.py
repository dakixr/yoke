from __future__ import annotations

from importlib import import_module
from typing import Any
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from yoke.agent.budget import ProviderCompactionBudget as ProviderCompactionBudget
    from yoke.agent.budget import (
        build_provider_context_manager as build_provider_context_manager,
    )
    from yoke.agent.budget import (
        current_context_fits_provider_budget as current_context_fits_provider_budget,
    )
    from yoke.agent.budget import (
        rebind_context_manager_budget as rebind_context_manager_budget,
    )
    from yoke.agent.budget import (
        resolve_provider_compaction_budget as resolve_provider_compaction_budget,
    )
    from yoke.agent.compaction import CompactionPolicy as CompactionPolicy
    from yoke.agent.compaction import CompactionPreparation as CompactionPreparation
    from yoke.agent.compaction import CompactionResult as CompactionResult
    from yoke.agent.compaction import ForcedCompaction as ForcedCompaction
    from yoke.agent.compaction import TokenEstimate as TokenEstimate
    from yoke.agent.compaction import (
        estimate_agent_context_usage as estimate_agent_context_usage,
    )
    from yoke.agent.compaction import force_compact_agent as force_compact_agent
    from yoke.agent.context import ContextManager as ContextManager
    from yoke.agent.loop import AfterToolCallContext as AfterToolCallContext
    from yoke.agent.loop import AfterToolCallResult as AfterToolCallResult
    from yoke.agent.loop import AgentResult as AgentResult
    from yoke.agent.loop import BeforeToolCallContext as BeforeToolCallContext
    from yoke.agent.loop import BeforeToolCallResult as BeforeToolCallResult
    from yoke.agent.loop import RuntimeAgent as RuntimeAgent
    from yoke.agent.models import AgentContext as AgentContext
    from yoke.agent.models import CompactionHandoff as CompactionHandoff
    from yoke.agent.models import ConversationEntry as ConversationEntry
    from yoke.agent.models import ConversationLog as ConversationLog
    from yoke.agent.models import MemorySnapshot as MemorySnapshot
    from yoke.agent.models import WorkingMemory as WorkingMemory
    from yoke.agent.prompting import PromptContext as PromptContext
    from yoke.agent.protocols import AgentRunner as AgentRunner
    from yoke.agent.state import AgentState as AgentState
    from yoke.agent.state import capture_agent_state as capture_agent_state
    from yoke.agent.state import hydrate_agent_state as hydrate_agent_state

_LAZY_EXPORTS = {
    "CompactionPolicy": ("yoke.agent.compaction", "CompactionPolicy"),
    "CompactionPreparation": ("yoke.agent.compaction", "CompactionPreparation"),
    "CompactionResult": ("yoke.agent.compaction", "CompactionResult"),
    "TokenEstimate": ("yoke.agent.compaction", "TokenEstimate"),
    "ContextManager": ("yoke.agent.context", "ContextManager"),
    "ProviderCompactionBudget": ("yoke.agent.budget", "ProviderCompactionBudget"),
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
    "AfterToolCallContext": ("yoke.agent.loop", "AfterToolCallContext"),
    "AfterToolCallResult": ("yoke.agent.loop", "AfterToolCallResult"),
    "AgentResult": ("yoke.agent.loop", "AgentResult"),
    "BeforeToolCallContext": ("yoke.agent.loop", "BeforeToolCallContext"),
    "BeforeToolCallResult": ("yoke.agent.loop", "BeforeToolCallResult"),
    "RuntimeAgent": ("yoke.agent.loop", "RuntimeAgent"),
    "AgentContext": ("yoke.agent.models", "AgentContext"),
    "CompactionHandoff": ("yoke.agent.models", "CompactionHandoff"),
    "ConversationEntry": ("yoke.agent.models", "ConversationEntry"),
    "ConversationLog": ("yoke.agent.models", "ConversationLog"),
    "MemorySnapshot": ("yoke.agent.models", "MemorySnapshot"),
    "WorkingMemory": ("yoke.agent.models", "WorkingMemory"),
    "ForcedCompaction": ("yoke.agent.compaction", "ForcedCompaction"),
    "estimate_agent_context_usage": (
        "yoke.agent.compaction",
        "estimate_agent_context_usage",
    ),
    "force_compact_agent": ("yoke.agent.compaction", "force_compact_agent"),
    "PromptContext": ("yoke.agent.prompting", "PromptContext"),
    "AgentRunner": ("yoke.agent.protocols", "AgentRunner"),
    "AgentState": ("yoke.agent.state", "AgentState"),
    "capture_agent_state": ("yoke.agent.state", "capture_agent_state"),
    "hydrate_agent_state": ("yoke.agent.state", "hydrate_agent_state"),
}

__all__ = [
    "AfterToolCallContext",
    "AfterToolCallResult",
    "RuntimeAgent",
    "AgentContext",
    "AgentResult",
    "BeforeToolCallContext",
    "BeforeToolCallResult",
    "CompactionPolicy",
    "CompactionPreparation",
    "CompactionResult",
    "CompactionHandoff",
    "ContextManager",
    "ConversationEntry",
    "ConversationLog",
    "MemorySnapshot",
    "PromptContext",
    "AgentState",
    "AgentRunner",
    "TokenEstimate",
    "WorkingMemory",
    "ForcedCompaction",
    "ProviderCompactionBudget",
    "build_provider_context_manager",
    "capture_agent_state",
    "current_context_fits_provider_budget",
    "estimate_agent_context_usage",
    "force_compact_agent",
    "hydrate_agent_state",
    "rebind_context_manager_budget",
    "resolve_provider_compaction_budget",
]


def __getattr__(name: str) -> Any:
    """Lazily resolve package re-exports."""
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value
