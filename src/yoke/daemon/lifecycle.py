"""Loop-owned daemon phases, client leases and guarded exit decisions."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Literal

from yoke.daemon.work import WorkSummary

DEFAULT_GRACE_SECONDS = 5.0
LOGGER = logging.getLogger(__name__)

type BusyProbe = Callable[[], Awaitable[WorkSummary | bool]]
type ExitCallback = Callable[[str], None]
type ExitReason = Literal["idle", "restart", "stop", "signal"]


class IllegalTransition(ValueError):
    """A daemon lifecycle event is illegal in the current phase."""


class PendingExit(StrEnum):
    RESTART = "restart"
    STOP = "stop"


@dataclass(frozen=True, slots=True)
class Serving:
    """Accept work with no pending exit."""


@dataclass(frozen=True, slots=True)
class Draining:
    """Accept work until it drains; leases do not prevent the requested exit."""

    pending: PendingExit


@dataclass(frozen=True, slots=True)
class Exiting:
    """Reject new mutations and retain the first exit reason."""

    reason: ExitReason


type DaemonPhase = Serving | Draining | Exiting


class LifecycleEvent(StrEnum):
    RESTART = "restart"
    STOP = "stop"
    CANCEL = "cancel"
    EXIT_IDLE = "exit_idle"
    EXIT_RESTART = "exit_restart"
    EXIT_STOP = "exit_stop"
    EXIT_SIGNAL = "exit_signal"


def next_phase(current: DaemonPhase, event: LifecycleEvent) -> DaemonPhase:
    """Apply one event; stop overrides restart and exit is terminal."""
    if not isinstance(current, Exiting):
        match event:
            case LifecycleEvent.RESTART:
                return (
                    current
                    if isinstance(current, Draining)
                    else Draining(PendingExit.RESTART)
                )
            case LifecycleEvent.STOP:
                return Draining(PendingExit.STOP)
            case LifecycleEvent.CANCEL:
                return Serving()
            case LifecycleEvent.EXIT_IDLE if isinstance(current, Serving):
                return Exiting("idle")
            case LifecycleEvent.EXIT_RESTART if (
                isinstance(current, Serving) or current.pending == PendingExit.RESTART
            ):
                return Exiting("restart")
            case LifecycleEvent.EXIT_STOP:
                return Exiting("stop")
            case LifecycleEvent.EXIT_SIGNAL:
                return Exiting("signal")
    raise IllegalTransition(f"{current!r} + {event.value}")


def phase_name(phase: DaemonPhase) -> Literal["serving", "draining", "exiting"]:
    match phase:
        case Serving():
            return "serving"
        case Draining():
            return "draining"
        case Exiting():
            return "exiting"


@dataclass(frozen=True, slots=True)
class Lease:
    id: str
    client: str
    pid: int | None
    opened_at: float


class DaemonLifecycle:
    """Own phase transitions and fence probes against concurrent admissions."""

    def __init__(
        self,
        *,
        idle_exit: bool = True,
        grace_seconds: float = DEFAULT_GRACE_SECONDS,
        poll_seconds: float = 0.2,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.idle_exit = idle_exit
        self.grace_seconds = grace_seconds
        self.poll_seconds = poll_seconds
        self._phase: DaemonPhase = Serving()
        self._leases: dict[str, Lease] = {}
        self._busy: BusyProbe | None = None
        self._on_exit: ExitCallback | None = None
        self._exiting: asyncio.Event | None = None
        self._mutations = 0
        self._admission_generation = 0
        self._idle_since: float | None = None
        self._clock = clock
        self._sleep = sleep

    @property
    def phase(self) -> DaemonPhase:
        return self._phase

    @property
    def restart_pending(self) -> bool:
        return (
            isinstance(self.phase, Draining)
            and self.phase.pending == PendingExit.RESTART
        )

    @property
    def exit_reason(self) -> ExitReason | None:
        return self.phase.reason if isinstance(self.phase, Exiting) else None

    def _apply(self, event: LifecycleEvent) -> None:
        previous = self.phase
        try:
            self._phase = next_phase(previous, event)
        except IllegalTransition:
            LOGGER.error(
                "Illegal daemon transition: phase=%r event=%s", previous, event
            )
            raise
        LOGGER.debug(
            "Daemon transition: previous=%r event=%s phase=%r",
            previous,
            event,
            self.phase,
        )
        self._idle_since = None
        if isinstance(self.phase, Exiting):
            self.exiting.set()
            if self._on_exit is not None:
                self._on_exit(self.phase.reason)

    def begin_mutation(self) -> None:
        """Keep an admitted HTTP mutation alive until its handler finishes."""
        if isinstance(self.phase, Exiting):
            LOGGER.error("Illegal daemon admission: phase=%r", self.phase)
            raise IllegalTransition("Cannot admit a mutation while exiting.")
        self._mutations += 1
        self._admission_generation += 1
        self._idle_since = None

    def end_mutation(self) -> None:
        if self._mutations <= 0:
            raise IllegalTransition("No mutation to finish.")
        self._mutations -= 1

    def bind_busy_probe(self, probe: BusyProbe) -> None:
        self._busy = probe

    def on_exit(self, callback: ExitCallback) -> None:
        self._on_exit = callback

    @property
    def exiting(self) -> asyncio.Event:
        """Set once the daemon decides to exit; lease streams end on it."""
        if self._exiting is None:
            self._exiting = asyncio.Event()
        return self._exiting

    def open_lease(self, client: str, pid: int | None = None) -> Lease:
        lease = Lease(
            id=f"lease_{secrets.token_hex(6)}",
            client=client,
            pid=pid,
            opened_at=time.time(),
        )
        self._leases[lease.id] = lease
        self._idle_since = None
        return lease

    def close_lease(self, lease: Lease) -> None:
        self._leases.pop(lease.id, None)

    def leases(self) -> list[Lease]:
        return list(self._leases.values())

    async def work_summary(self) -> WorkSummary:
        generation = self._admission_generation
        work = await self._busy() if self._busy is not None else WorkSummary()
        # Retain boolean probes for in-process integrations.
        if isinstance(work, bool):
            work = WorkSummary(running_sessions=("unknown",) if work else ())
        return replace(
            work,
            mutations=self._mutations,
            admission_changed=generation != self._admission_generation,
        )

    async def busy(self) -> bool:
        return (await self.work_summary()).busy

    def request_exit(self, reason: ExitReason) -> None:
        """Begin shutdown once; later requests keep the first reason."""
        if isinstance(self.phase, Exiting):
            return
        self._apply(LifecycleEvent(f"exit_{reason}"))

    def request_restart_when_idle(self) -> None:
        self._apply(LifecycleEvent.RESTART)

    def request_stop_when_idle(self) -> None:
        self._apply(LifecycleEvent.STOP)

    def cancel_pending_exit(self) -> None:
        self._apply(LifecycleEvent.CANCEL)

    async def watch(self) -> None:
        """Commit an exit only after a fresh probe with no intervening admission."""
        while not isinstance(self.phase, Exiting):
            await self._sleep(self.poll_seconds)
            if isinstance(self.phase, Exiting):
                return
            if not self.idle_exit and isinstance(self.phase, Serving):
                continue
            work = await self.work_summary()
            # A control request can replace or cancel a drain during the probe.
            if isinstance(self.phase, Exiting):
                return
            if isinstance(self.phase, Draining) and not work.busy:
                self.request_exit(self.phase.pending.value)
                return
            if not self.idle_exit or work.busy or self._leases:
                self._idle_since = None
                continue
            now = self._clock()
            if self._idle_since is None:
                self._idle_since = now
            elif now - self._idle_since >= self.grace_seconds:
                self.request_exit("idle")
                return
