"""The explicit lifecycle of one observed agent run.

`lost` means the host stopped hearing from a run that never reported a
terminal status. It is terminal for retention: the record keeps no evidence
that execution ended, so the public status stays `running`. A heartbeat can
still revive a run that has not been evicted yet.
"""

from __future__ import annotations

from enum import StrEnum


class RunPhase(StrEnum):
    """Where one agent run is in its observed lifetime."""

    RUNNING = "running"
    LOST = "lost"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class RunEvent(StrEnum):
    """Inputs that can move an observed run."""

    HEARTBEAT = "heartbeat"
    STALE = "stale"
    LOST = "lost"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class IllegalRunTransition(Exception):
    """Raised for a move that the run lifecycle does not allow."""

    def __init__(self, phase: RunPhase, event: RunEvent) -> None:
        self.phase = phase
        self.event = event
        super().__init__(f"Run in phase {phase.value} cannot handle {event.value!r}")


TERMINAL = {
    RunPhase.COMPLETED,
    RunPhase.FAILED,
    RunPhase.CANCELLED,
    RunPhase.INTERRUPTED,
}

_LEGAL: dict[RunPhase, dict[RunEvent, RunPhase]] = {
    RunPhase.RUNNING: {
        RunEvent.HEARTBEAT: RunPhase.RUNNING,
        RunEvent.STALE: RunPhase.RUNNING,
        RunEvent.LOST: RunPhase.LOST,
        RunEvent.COMPLETED: RunPhase.COMPLETED,
        RunEvent.FAILED: RunPhase.FAILED,
        RunEvent.CANCELLED: RunPhase.CANCELLED,
        RunEvent.INTERRUPTED: RunPhase.INTERRUPTED,
    },
    RunPhase.LOST: {
        RunEvent.HEARTBEAT: RunPhase.RUNNING,
        RunEvent.COMPLETED: RunPhase.COMPLETED,
        RunEvent.FAILED: RunPhase.FAILED,
        RunEvent.CANCELLED: RunPhase.CANCELLED,
        RunEvent.INTERRUPTED: RunPhase.INTERRUPTED,
        RunEvent.LOST: RunPhase.LOST,
    },
}


def next_run_phase(phase: RunPhase, event: RunEvent) -> RunPhase:
    """Apply one event. Every unlisted move raises IllegalRunTransition."""
    try:
        return _LEGAL[phase][event]
    except KeyError:
        raise IllegalRunTransition(phase, event) from None


def is_terminal(phase: RunPhase) -> bool:
    """Return whether this phase is retention-terminal."""
    return phase in TERMINAL or phase is RunPhase.LOST


def public_observation(phase: RunPhase, observation: str) -> str:
    """Project the phase onto the existing `observation` wire vocabulary."""
    return "lost" if phase is RunPhase.LOST else observation
