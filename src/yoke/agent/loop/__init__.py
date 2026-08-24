"""Lazy public exports for the agent loop."""

# ruff: noqa: F401

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .agent import RuntimeAgent
    from .types import (
        INTERRUPTED_TURN_NOTICE,
        AfterToolCallContext,
        AfterToolCallHook,
        AfterToolCallResult,
        AgentEventHandler,
        AgentResult,
        AgentStoppedError,
        BeforeToolCallContext,
        BeforeToolCallHook,
        BeforeToolCallResult,
        StopRequested,
        ToolExecutionMode,
    )

_TYPE_EXPORTS = (
    "INTERRUPTED_TURN_NOTICE",
    "AfterToolCallContext",
    "AfterToolCallHook",
    "AfterToolCallResult",
    "AgentEventHandler",
    "AgentResult",
    "AgentStoppedError",
    "BeforeToolCallContext",
    "BeforeToolCallHook",
    "BeforeToolCallResult",
    "StopRequested",
    "ToolExecutionMode",
)

_LAZY_EXPORTS = {
    "RuntimeAgent": ("yoke.agent.loop.agent", "RuntimeAgent"),
    **{name: ("yoke.agent.loop.types", name) for name in _TYPE_EXPORTS},
}

__all__ = (
    "RuntimeAgent",
    "INTERRUPTED_TURN_NOTICE",
    "AfterToolCallContext",
    "AfterToolCallHook",
    "AfterToolCallResult",
    "AgentEventHandler",
    "AgentResult",
    "AgentStoppedError",
    "BeforeToolCallContext",
    "BeforeToolCallHook",
    "BeforeToolCallResult",
    "StopRequested",
    "ToolExecutionMode",
)


def __getattr__(name: str) -> Any:  # noqa: ANN401
    """Resolve a loop export without eagerly importing the full runtime."""
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value
