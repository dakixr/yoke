"""Pure runtime phases and their legal transitions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from yoke.http.runtime.turns import SessionOperation, TurnExecution


class IllegalTransition(ValueError):
    """A lifecycle transition would violate ownership or ordering."""


class Activity(StrEnum):
    """Stable display values for turn activity."""

    WAITING_SLOT = "Waiting for a slot"
    LOADING = "Loading session"
    THINKING = "Thinking"
    TOOL = "Running tool"
    WAITING_INPUT = "Waiting for input"
    STREAMING = "Streaming"
    RATE_LIMITED = "Rate limited"
    RETRYING = "Retrying provider"
    COMPACTING = "Compacting"
    RECOVERING = "Recovering"


@dataclass(frozen=True, slots=True)
class Idle:
    """No logical work owns the session lane."""

    last_error: str | None = None
    hold: str | None = None


@dataclass(frozen=True, slots=True)
class TurnActive:
    """One execution owns the lane, including capacity and human waits."""

    execution: TurnExecution
    activity: Activity


@dataclass(frozen=True, slots=True)
class Retiring:
    """A fenced execution is saving its interruption."""

    execution: TurnExecution
    reason: Literal["user", "steering", "shutdown"]


@dataclass(frozen=True, slots=True)
class OperationActive:
    """A model selection or manual compaction owns the lane."""

    operation: SessionOperation


type RuntimePhase = Idle | TurnActive | Retiring | OperationActive
type RuntimeState = Literal["idle", "running", "stopping", "waiting_input", "error"]

# Idle -> Idle updates an idle error or clears it after workspace relocation.
RUNTIME_TRANSITIONS: dict[type[RuntimePhase], frozenset[type[RuntimePhase]]] = {
    Idle: frozenset({Idle, TurnActive, OperationActive}),
    TurnActive: frozenset({TurnActive, Retiring, Idle}),
    Retiring: frozenset({Idle, TurnActive}),
    OperationActive: frozenset({Idle, TurnActive}),
}


def next_phase(current: RuntimePhase, new: RuntimePhase) -> RuntimePhase:
    """Validate an edge and preserve the identity of an active execution."""
    if type(new) not in RUNTIME_TRANSITIONS[type(current)]:
        raise IllegalTransition(f"{phase_name(current)} -> {phase_name(new)}")
    if isinstance(current, TurnActive) and isinstance(new, (TurnActive, Retiring)):
        if current.execution is not new.execution:
            raise IllegalTransition("An active execution cannot change identity.")
    return new


def phase_name(
    phase: RuntimePhase,
) -> Literal["idle", "turn_active", "retiring", "operation_active"]:
    """Return the additive, stable wire phase name."""
    match phase:
        case Idle():
            return "idle"
        case TurnActive():
            return "turn_active"
        case Retiring():
            return "retiring"
        case OperationActive():
            return "operation_active"


def wire_state(phase: RuntimePhase) -> RuntimeState:
    """Project every phase onto the protocol 2 state vocabulary."""
    match phase:
        case Idle(last_error=error):
            return "error" if error else "idle"
        case Retiring():
            return "stopping"
        case TurnActive(activity=Activity.WAITING_INPUT):
            return "waiting_input"
        case _:
            return "running"
