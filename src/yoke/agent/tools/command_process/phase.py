"""The explicit lifecycle of one managed command process.

The process object owns the phase. Local resource release is separate: a
process that exited can still hold open descriptors until cleanup runs.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import Literal

LOGGER = logging.getLogger(__name__)


class ProcessPhase(StrEnum):
    """Where one managed child process is in its lifetime."""

    STARTING = "starting"
    RUNNING = "running"
    EXITING = "exiting"
    EXITED = "exited"
    CLEANUP_FAILED = "cleanup_failed"


class ProcessEvent(StrEnum):
    """Inputs that can move a managed process."""

    START = "start"
    EXIT = "exit"
    SHUTDOWN = "shutdown"
    CLEANUP_COMPLETE = "cleanup_complete"
    CLEANUP_FAILED = "cleanup_failed"
    RETRY = "retry"


class IllegalProcessTransition(Exception):
    """Raised for a move that the process lifecycle does not allow."""

    def __init__(self, phase: ProcessPhase, event: ProcessEvent) -> None:
        self.phase = phase
        self.event = event
        super().__init__(
            f"Process in phase {phase.value} cannot handle {event.value!r}"
        )


_LEGAL: dict[ProcessPhase, dict[ProcessEvent, ProcessPhase]] = {
    ProcessPhase.STARTING: {
        ProcessEvent.START: ProcessPhase.RUNNING,
        ProcessEvent.EXIT: ProcessPhase.EXITED,
        ProcessEvent.SHUTDOWN: ProcessPhase.EXITING,
    },
    ProcessPhase.RUNNING: {
        ProcessEvent.EXIT: ProcessPhase.EXITED,
        ProcessEvent.SHUTDOWN: ProcessPhase.EXITING,
    },
    ProcessPhase.EXITING: {
        ProcessEvent.CLEANUP_COMPLETE: ProcessPhase.EXITED,
        ProcessEvent.CLEANUP_FAILED: ProcessPhase.CLEANUP_FAILED,
    },
    ProcessPhase.EXITED: {
        ProcessEvent.SHUTDOWN: ProcessPhase.EXITED,
        ProcessEvent.CLEANUP_COMPLETE: ProcessPhase.EXITED,
        ProcessEvent.CLEANUP_FAILED: ProcessPhase.CLEANUP_FAILED,
    },
    ProcessPhase.CLEANUP_FAILED: {
        ProcessEvent.RETRY: ProcessPhase.EXITING,
    },
}

TERMINAL = {ProcessPhase.EXITED, ProcessPhase.CLEANUP_FAILED}


def next_process_phase(phase: ProcessPhase, event: ProcessEvent) -> ProcessPhase:
    """Apply one event. Every unlisted move raises IllegalProcessTransition."""
    try:
        return _LEGAL[phase][event]
    except KeyError:
        raise IllegalProcessTransition(phase, event) from None


def process_status(
    phase: ProcessPhase, exit_code: int | None
) -> Literal["running", "exited", "failed"]:
    """Project the phase onto the stable snapshot status clients already read."""
    if phase is ProcessPhase.EXITED:
        return "exited" if exit_code == 0 else "failed"
    if phase is ProcessPhase.CLEANUP_FAILED:
        # A failed cleanup is visible, and it never counts as running work.
        return "failed"
    return "running"
