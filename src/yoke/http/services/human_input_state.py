"""The explicit lifecycle of one permission or question request.

The transition table is pure data plus a pure function. The service holds the
lock and performs I/O around it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from yoke.http.models.human_input import ResolvedBy


class RequestEvent(StrEnum):
    """Inputs that can move a request."""

    CREATE = "create"
    ANSWER = "answer"
    CANCEL = "cancel"
    EXPIRE = "expire"
    CONSUME = "consume"


class RequestPhase(StrEnum):
    """The wire-visible phase used in conflict details and tests."""

    PENDING = "pending"
    ANSWERED = "answered"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    CONSUMED = "consumed"


@dataclass(frozen=True, slots=True)
class Pending:
    """A worker waits and no outcome is recorded yet."""


@dataclass(frozen=True, slots=True)
class Answered:
    """A human replied first. The value is the stored resolution."""

    value: object


@dataclass(frozen=True, slots=True)
class Cancelled:
    """A turn stop or session close ended the wait with a resolution."""

    by: ResolvedBy
    value: object


@dataclass(frozen=True, slots=True)
class Expired:
    """No reply arrived before the request's deadline."""


@dataclass(frozen=True, slots=True)
class Consumed:
    """The waiting worker took the result. The tombstone stays for conflicts."""

    previous: Answered | Cancelled | Expired


type RequestState = Pending | Answered | Cancelled | Expired | Consumed


class IllegalRequestTransition(Exception):
    """Raised for a move that the request lifecycle does not allow."""

    def __init__(self, state: RequestState | None, event: RequestEvent) -> None:
        self.state = state
        self.event = event
        super().__init__(
            f"Request in phase {phase_of(state).value} cannot handle {event.value!r}"
        )


def phase_of(state: RequestState | None) -> RequestPhase:
    """Return the coarse phase of a state, or PENDING for a missing one."""
    match state:
        case None | Pending():
            return RequestPhase.PENDING
        case Answered():
            return RequestPhase.ANSWERED
        case Cancelled():
            return RequestPhase.CANCELLED
        case Expired():
            return RequestPhase.EXPIRED
        case Consumed():
            return RequestPhase.CONSUMED


def resolution_of(state: RequestState | None) -> object | None:
    """Return the stored resolution, including one kept by a tombstone."""
    match state:
        case Answered(value=value) | Cancelled(value=value):
            return value
        case Consumed(previous=Answered(value=value)):
            return value
        case Consumed(previous=Cancelled(value=value)):
            return value
        case _:
            return None


def next_request_state(
    state: RequestState | None,
    event: RequestEvent,
    *,
    value: object | None = None,
    by: ResolvedBy | None = None,
) -> RequestState:
    """Apply one event. Every unlisted move raises IllegalRequestTransition."""
    if event is RequestEvent.CREATE:
        if state is not None:
            raise IllegalRequestTransition(state, event)
        return Pending()
    if state is None:
        raise IllegalRequestTransition(state, event)
    if isinstance(state, Pending):
        if event is RequestEvent.ANSWER:
            if value is None or by is not ResolvedBy.USER:
                raise IllegalRequestTransition(state, event)
            return Answered(value)
        if event is RequestEvent.CANCEL:
            if (
                value is None
                or by is None
                or by
                not in {
                    ResolvedBy.TURN_STOPPED,
                    ResolvedBy.SESSION_CLOSED,
                }
            ):
                raise IllegalRequestTransition(state, event)
            return Cancelled(by, value)
        if event is RequestEvent.EXPIRE:
            return Expired()
    elif isinstance(state, (Answered, Cancelled, Expired)):
        if event is RequestEvent.CONSUME:
            return Consumed(state)
    raise IllegalRequestTransition(state, event)
