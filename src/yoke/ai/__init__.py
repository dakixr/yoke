"""Small public SDK surface for embedding yoke in Python code.

Typical usage:

```python
from yoke.ai import Agent, build_builtin_provider

provider = build_builtin_provider("codex:gpt-5.6-sol:medium")

agent = Agent(provider=provider)

result = agent.prompt("Create hello.py")
print(result.output)
```
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any

if TYPE_CHECKING:
    from yoke.agent import AgentState
    from yoke.agent import AgentStateLoadError
    from yoke.agent import AgentStatePersistenceError
    from yoke.agent import AgentStateSaveError
    from yoke.agent import AgentStateSnapshot
    from yoke.ai.sdk import Agent
    from yoke.ai.sdk import AgentObserver
    from yoke.ai.sdk import AgentTraceEvent
    from yoke.ai.sdk import BatchItemResult
    from yoke.ai.sdk import BatchProgress
    from yoke.ai.sdk import BatchResult
    from yoke.ai.sdk import BatchTask
    from yoke.ai.sdk import BatchUsage
    from yoke.ai.sdk import CompositeObserver
    from yoke.ai.sdk import Image
    from yoke.ai.sdk import ConsoleObserver
    from yoke.ai.sdk import JsonlObserver
    from yoke.ai.sdk import LoggingObserver
    from yoke.ai.sdk import RunConfig
    from yoke.ai.sdk import TraceDetail
    from yoke.ai.sdk import complete
    from yoke.ai.sdk import run_many
    from yoke.ai.sdk.providers import available_builtin_providers
    from yoke.ai.sdk.providers import build_builtin_provider

_AGENT_EXPORTS = {
    "AgentState",
    "AgentStateLoadError",
    "AgentStatePersistenceError",
    "AgentStateSaveError",
    "AgentStateSnapshot",
}

_SDK_EXPORTS = {
    "Agent",
    "AgentObserver",
    "AgentTraceEvent",
    "BatchItemResult",
    "BatchProgress",
    "BatchResult",
    "BatchTask",
    "BatchUsage",
    "CompositeObserver",
    "ConsoleObserver",
    "Image",
    "JsonlObserver",
    "LoggingObserver",
    "RunConfig",
    "TraceDetail",
    "complete",
    "run_many",
}

_PROVIDER_EXPORTS = {
    "available_builtin_providers",
    "build_builtin_provider",
}


def __getattr__(name: str) -> Any:  # noqa: ANN401
    if name in _AGENT_EXPORTS:
        from yoke import agent

        value = getattr(agent, name)
    elif name in _SDK_EXPORTS:
        from yoke.ai import sdk

        value = getattr(sdk, name)
    elif name in _PROVIDER_EXPORTS:
        from yoke.ai.sdk import providers

        value = getattr(providers, name)
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value


__all__ = [
    "Agent",
    "AgentObserver",
    "AgentState",
    "AgentStateLoadError",
    "AgentStatePersistenceError",
    "AgentStateSaveError",
    "AgentStateSnapshot",
    "AgentTraceEvent",
    "available_builtin_providers",
    "BatchItemResult",
    "BatchProgress",
    "BatchResult",
    "BatchTask",
    "BatchUsage",
    "build_builtin_provider",
    "CompositeObserver",
    "ConsoleObserver",
    "Image",
    "JsonlObserver",
    "LoggingObserver",
    "RunConfig",
    "TraceDetail",
    "complete",
    "run_many",
]
