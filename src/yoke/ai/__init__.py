"""Lazy public exports; generate the matching stub with docs/tools/generate_exports.py."""

from __future__ import annotations
from importlib import import_module
from typing import Any

_LAZY_EXPORTS = {
    "Agent": "yoke.ai.sdk",
    "AgentObserver": "yoke.ai.sdk",
    "AgentState": "yoke.agent",
    "AgentStateLoadError": "yoke.agent",
    "AgentStatePersistenceError": "yoke.agent",
    "AgentStateSaveError": "yoke.agent",
    "AgentStateSnapshot": "yoke.agent",
    "AgentTraceEvent": "yoke.ai.sdk",
    "available_builtin_providers": "yoke.ai.sdk.providers",
    "BatchItemResult": "yoke.ai.sdk",
    "BatchProgress": "yoke.ai.sdk",
    "BatchResult": "yoke.ai.sdk",
    "BatchTask": "yoke.ai.sdk",
    "BatchUsage": "yoke.ai.sdk",
    "build_builtin_provider": "yoke.ai.sdk.providers",
    "CompositeObserver": "yoke.ai.sdk",
    "ConsoleObserver": "yoke.ai.sdk",
    "Image": "yoke.ai.sdk",
    "JsonlObserver": "yoke.ai.sdk",
    "LoggingObserver": "yoke.ai.sdk",
    "RunConfig": "yoke.ai.sdk",
    "TraceDetail": "yoke.ai.sdk",
    "complete": "yoke.ai.sdk",
    "run_many": "yoke.ai.sdk",
}

__all__ = list(_LAZY_EXPORTS)  # pyright: ignore[reportUnsupportedDunderAll]


def __getattr__(name: str) -> Any:
    """Resolve an export on first access, retaining normal module identity."""
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = (target, name) if isinstance(target, str) else target
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value
