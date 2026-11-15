"""Lazy public exports; generate the matching stub with docs/tools/generate_exports.py."""

from __future__ import annotations

from importlib import import_module
from typing import Any

_LAZY_EXPORTS = {
    "RuntimeAgent": "yoke.agent.loop.agent",
    "INTERRUPTED_TURN_NOTICE": "yoke.agent.loop.types",
    "AfterToolCallContext": "yoke.agent.loop.types",
    "AfterToolCallHook": "yoke.agent.loop.types",
    "AfterToolCallResult": "yoke.agent.loop.types",
    "AgentEventHandler": "yoke.agent.loop.types",
    "AgentResult": "yoke.agent.loop.types",
    "AgentStoppedError": "yoke.agent.loop.types",
    "BeforeToolCallContext": "yoke.agent.loop.types",
    "BeforeToolCallHook": "yoke.agent.loop.types",
    "BeforeToolCallResult": "yoke.agent.loop.types",
    "StopRequested": "yoke.agent.loop.types",
    "ToolExecutionMode": "yoke.agent.loop.types",
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
