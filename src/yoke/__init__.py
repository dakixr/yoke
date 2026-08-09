from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

from yoke._version import __version__

if TYPE_CHECKING:
    from yoke.agent import AfterToolCallContext
    from yoke.agent import AfterToolCallResult
    from yoke.agent import AgentContext
    from yoke.agent import AgentResult
    from yoke.agent import BeforeToolCallContext
    from yoke.agent import BeforeToolCallResult
    from yoke.agent import CompactionPolicy
    from yoke.agent import ContextManager
    from yoke.agent import RuntimeAgent
    from yoke.agent.skills import load_skill_registry

_AGENT_EXPORTS = frozenset(
    {
        "AfterToolCallContext",
        "AfterToolCallResult",
        "RuntimeAgent",
        "AgentContext",
        "AgentResult",
        "BeforeToolCallContext",
        "BeforeToolCallResult",
        "CompactionPolicy",
        "ContextManager",
    }
)


def __getattr__(name: str) -> Any:  # noqa: ANN401
    """Load public SDK exports only when callers request them."""
    if name in _AGENT_EXPORTS:
        from yoke import agent

        value = getattr(agent, name)
        globals()[name] = value
        return value
    if name == "load_skill_registry":
        from yoke.agent.skills import load_skill_registry

        globals()[name] = load_skill_registry
        return load_skill_registry
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


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
