"""Typed input phases and the complete, side-effect-free transition table."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field


type Outcome = Literal["completed", "stopped", "failed", "recovered"]


class PauseCause(StrEnum):
    USER = "user"
    WORKSPACE = "workspace"


@dataclass(frozen=True, slots=True)
class Queued:
    position: int
    kind: Literal["queued"] = "queued"


@dataclass(frozen=True, slots=True)
class Paused:
    position: int
    cause: PauseCause
    kind: Literal["paused"] = "paused"


@dataclass(frozen=True, slots=True)
class Promoted:
    promoted_seq: int
    queue_position: int
    kind: Literal["promoted"] = "promoted"


@dataclass(frozen=True, slots=True)
class Running:
    promoted_seq: int
    queue_position: int
    turn_id: int
    kind: Literal["running"] = "running"


@dataclass(frozen=True, slots=True)
class Settled:
    outcome: Outcome
    settled_at: str
    kind: Literal["settled"] = "settled"


@dataclass(frozen=True, slots=True)
class Removed:
    kind: Literal["removed"] = "removed"


type InputPhase = Annotated[
    Queued | Paused | Promoted | Running | Settled | Removed,
    Field(discriminator="kind"),
]


class InputEvent(StrEnum):
    ADMIT = "admit"
    EDIT = "edit"
    PAUSE = "pause"
    RESUME = "resume"
    MOVE = "move"
    REMOVE = "remove"
    PROMOTE = "promote"
    START = "start"
    BLOCK_WORKSPACE = "block_workspace"
    SETTLE = "settle"
    RECOVER_RESUME = "recover_resume"
    RECOVER_SETTLE = "recover_settle"


class IllegalTransition(ValueError):
    """An input event cannot cross this phase boundary."""


TRANSITIONS = {
    (None, InputEvent.ADMIT): Queued,
    (Queued, InputEvent.PAUSE): Paused,
    (Paused, InputEvent.RESUME): Queued,
    (Queued, InputEvent.EDIT): Queued,
    (Paused, InputEvent.EDIT): Paused,
    (Queued, InputEvent.MOVE): Queued,
    (Paused, InputEvent.MOVE): Paused,
    (Queued, InputEvent.REMOVE): Removed,
    (Paused, InputEvent.REMOVE): Removed,
    (Queued, InputEvent.PROMOTE): Promoted,
    (Promoted, InputEvent.START): Running,
    (Promoted, InputEvent.BLOCK_WORKSPACE): Paused,
    (Running, InputEvent.BLOCK_WORKSPACE): Paused,
    (Paused, InputEvent.BLOCK_WORKSPACE): Paused,
    (Promoted, InputEvent.SETTLE): Settled,
    (Running, InputEvent.SETTLE): Settled,
    (Promoted, InputEvent.RECOVER_RESUME): Promoted,
    (Running, InputEvent.RECOVER_RESUME): Promoted,
    (Promoted, InputEvent.RECOVER_SETTLE): Settled,
    (Running, InputEvent.RECOVER_SETTLE): Settled,
}


def next_phase(
    current: InputPhase | None, event: InputEvent, target: InputPhase
) -> InputPhase:
    """Reject every unlisted edge and invalid state payload."""
    expected = TRANSITIONS.get((type(current) if current else None, event))
    if expected is not type(target):
        raise IllegalTransition(f"{current!r} cannot {event} to {target!r}")
    if isinstance(target, (Queued, Paused)) and target.position < 0:
        raise IllegalTransition("Queue position must be nonnegative")
    if (
        event == InputEvent.BLOCK_WORKSPACE
        and isinstance(current, Paused)
        and current.cause != PauseCause.WORKSPACE
    ):
        raise IllegalTransition("Only a workspace pause can be repaired")
    if event == InputEvent.BLOCK_WORKSPACE and (
        not isinstance(target, Paused) or target.cause != PauseCause.WORKSPACE
    ):
        raise IllegalTransition("Workspace blocking requires a workspace pause")
    if event == InputEvent.PAUSE and (
        not isinstance(target, Paused) or target.cause != PauseCause.USER
    ):
        raise IllegalTransition("User pause requires a user cause")
    if event == InputEvent.RECOVER_SETTLE and (
        not isinstance(target, Settled) or target.outcome != "recovered"
    ):
        raise IllegalTransition("Recovery requires the recovered outcome")
    return target
