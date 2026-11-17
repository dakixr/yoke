"""Connection transitions and session-scoped recovery cursors."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from enum import StrEnum
from threading import Condition

LOGGER = logging.getLogger(__name__)


class ConnectionPhase(StrEnum):
    DISCOVERING = "discovering"
    ATTACHING = "attaching"
    STREAMING = "streaming"
    SYNCING = "syncing"
    READY = "ready"
    DISCONNECTED = "disconnected"
    CLOSED = "closed"


EDGES = {
    ConnectionPhase.DISCOVERING: {
        ConnectionPhase.ATTACHING,
        ConnectionPhase.DISCONNECTED,
    },
    ConnectionPhase.ATTACHING: {
        ConnectionPhase.STREAMING,
        ConnectionPhase.DISCONNECTED,
    },
    ConnectionPhase.STREAMING: {ConnectionPhase.SYNCING, ConnectionPhase.DISCONNECTED},
    ConnectionPhase.SYNCING: {ConnectionPhase.READY, ConnectionPhase.DISCONNECTED},
    ConnectionPhase.READY: {ConnectionPhase.SYNCING, ConnectionPhase.DISCONNECTED},
    ConnectionPhase.DISCONNECTED: {ConnectionPhase.DISCOVERING},
    ConnectionPhase.CLOSED: set(),
}


@dataclass(frozen=True, slots=True)
class ConnectionSnapshot:
    session_id: str
    phase: ConnectionPhase = ConnectionPhase.DISCOVERING
    generation: int = 0
    epoch: int = 0
    seq: int = 0


class ConnectionState:
    """Atomically bind identity, cursor, and readiness under one condition."""

    def __init__(self, session_id: str) -> None:
        self._condition = Condition()
        self._value = ConnectionSnapshot(session_id)

    @property
    def snapshot(self) -> ConnectionSnapshot:
        with self._condition:
            return self._value

    @property
    def closed(self) -> bool:
        return self.snapshot.phase == ConnectionPhase.CLOSED

    def is_set(self) -> bool:
        """Compatibility with integrations that inspected the former close event."""
        return self.closed

    def enter(self, phase: ConnectionPhase, *, cause: str) -> ConnectionSnapshot:
        with self._condition:
            old = self._value
            if old.phase == phase:
                return old
            if old.phase == ConnectionPhase.CLOSED or (
                phase != ConnectionPhase.CLOSED and phase not in EDGES[old.phase]
            ):
                raise ValueError(
                    f"Illegal connection transition: {old.phase} -> {phase}"
                )
            self._value = replace(
                old,
                phase=phase,
                generation=old.generation + (phase == ConnectionPhase.READY),
            )
            LOGGER.debug(
                "machine=cli_connection id=%s from=%s to=%s cause=%s generation=%s",
                old.session_id,
                old.phase,
                phase,
                cause,
                self._value.generation,
            )
            self._condition.notify_all()
            return self._value

    def switch_session(self, session_id: str) -> None:
        with self._condition:
            if self.closed:
                raise RuntimeError("Session connection is closed.")
            self._value = ConnectionSnapshot(
                session_id,
                ConnectionPhase.DISCONNECTED,
                self._value.generation,
                self._value.epoch + 1,
            )
            self._condition.notify_all()

    def advance(self, session_id: str, seq: int) -> bool:
        with self._condition:
            if session_id != self._value.session_id or seq <= self._value.seq:
                return False
            self._value = replace(self._value, seq=seq)
            return True

    def wait_ready(
        self, after: int | None = None, *, timeout: float = 10, epoch: int | None = None
    ) -> int:
        with self._condition:
            ready = self._condition.wait_for(
                lambda: (
                    self.closed
                    or (epoch is not None and self._value.epoch != epoch)
                    or (
                        self._value.phase == ConnectionPhase.READY
                        and (after is None or self._value.generation > after)
                    )
                ),
                timeout,
            )
            if epoch is not None and self._value.epoch != epoch:
                raise RuntimeError("Session changed during request recovery.")
            if not ready or self.closed:
                raise TimeoutError("Could not reattach to Yoke daemon.")
            return self._value.generation
