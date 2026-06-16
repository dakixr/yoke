from __future__ import annotations

from importlib import import_module
from typing import Any
from typing import TYPE_CHECKING

from yoke._version import __version__

if TYPE_CHECKING:
    from yoke.agent import AfterToolCallContext as AfterToolCallContext
    from yoke.agent import AfterToolCallResult as AfterToolCallResult
    from yoke.agent import AgentContext as AgentContext
    from yoke.agent import AgentResult as AgentResult
    from yoke.agent import BeforeToolCallContext as BeforeToolCallContext
    from yoke.agent import BeforeToolCallResult as BeforeToolCallResult
    from yoke.agent import CompactionPolicy as CompactionPolicy
    from yoke.agent import ContextManager as ContextManager
    from yoke.agent import RuntimeAgent as RuntimeAgent
    from yoke.agent.skills import load_skill_registry as load_skill_registry

_LAZY_EXPORTS = {
    "AfterToolCallContext": ("yoke.agent", "AfterToolCallContext"),
    "AfterToolCallResult": ("yoke.agent", "AfterToolCallResult"),
    "RuntimeAgent": ("yoke.agent", "RuntimeAgent"),
    "AgentContext": ("yoke.agent", "AgentContext"),
    "AgentResult": ("yoke.agent", "AgentResult"),
    "BeforeToolCallContext": ("yoke.agent", "BeforeToolCallContext"),
    "BeforeToolCallResult": ("yoke.agent", "BeforeToolCallResult"),
    "CompactionPolicy": ("yoke.agent", "CompactionPolicy"),
    "ContextManager": ("yoke.agent", "ContextManager"),
    "load_skill_registry": ("yoke.agent.skills", "load_skill_registry"),
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
    "ContextManager",
    "load_skill_registry",
    "__version__",
]


def __getattr__(name: str) -> Any:
    """Lazily resolve public SDK symbols without slowing CLI startup."""
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attribute = target
    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value
