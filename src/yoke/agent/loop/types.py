"""Public loop types and constants."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from yoke.agent.compaction import CompactionPreparation
from yoke.agent.compaction import CompactionResult
from yoke.agent.models import AgentContext
from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.agent.models import ToolCall


@dataclass(slots=True)
class AgentResult:
    """Result returned after an agent completes a prompt."""

    output: str
    messages: list[Message]
    iterations: int
    status: Literal["completed", "stopped"] = "completed"
    conversation_entries: list[ConversationEntry] | None = None


@dataclass(slots=True)
class BeforeToolCallContext:
    """Context passed to the before_tool_call hook."""

    iteration: int
    tool_call: ToolCall
    arguments: dict[str, object]
    context: AgentContext


@dataclass(slots=True)
class BeforeToolCallResult:
    """Result returned from the before_tool_call hook."""

    block: bool = False
    reason: str | None = None
    arguments: dict[str, object] | None = None


@dataclass(slots=True)
class AfterToolCallContext:
    """Context passed to the after_tool_call hook."""

    iteration: int
    tool_call: ToolCall
    arguments: dict[str, object]
    result: dict[str, object]
    context: AgentContext


@dataclass(slots=True)
class AfterToolCallResult:
    """Result returned from the after_tool_call hook."""

    result: dict[str, object] | None = None


@dataclass(slots=True)
class PreparedToolCall:
    """A validated tool call ready for execution."""

    tool_call: ToolCall
    arguments: dict[str, object]


@dataclass(slots=True)
class ImmediateToolResult:
    """A tool result that is immediately available without execution."""

    tool_call: ToolCall
    result: dict[str, object]


@dataclass(slots=True)
class CompactionAttempt:
    """Outcome of an attempted context compaction within the agent loop."""

    result: CompactionResult | None = None
    failed: bool = False
    preparation: CompactionPreparation | None = None


class MaxIterationsExceededError(RuntimeError):
    """Raised when the agent exceeds its configured myokemum iteration count."""

    partial_messages: list[Message] | None
    partial_conversation_entries: list[ConversationEntry] | None

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.partial_messages = None
        self.partial_conversation_entries = None


class AgentStoppedError(RuntimeError):
    """Raised when the agent is stopped via the stop_requested callback."""


INTERRUPTED_TURN_NOTICE = (
    "The previous turn was interrupted by the user before completion. Continue "
    "from the current state and follow the user's next instruction."
)
AgentEventHandler = Callable[[str, dict[str, object]], None]
BeforeToolCallHook = Callable[[BeforeToolCallContext], BeforeToolCallResult | None]
AfterToolCallHook = Callable[[AfterToolCallContext], AfterToolCallResult | None]
StopRequested = Callable[[], bool]
ToolExecutionMode = Literal["parallel", "sequential"]
