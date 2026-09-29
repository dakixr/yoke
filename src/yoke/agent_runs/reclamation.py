"""Bounded ownership reservations and retries for remote authority release."""

from __future__ import annotations

from collections.abc import Callable
import threading

from yoke.agent_runs.protocol import RegistrationError


class Revocations:
    """Reserve cleanup capacity before acquisition, retain it until release ACK."""

    def __init__(self, *, limit: int = 4096, retry_seconds: float = 1) -> None:
        self._limit = limit
        self._retry_seconds = retry_seconds
        self._condition = threading.Condition()
        self._reserved: set[object] = set()
        self._pending: dict[object, Callable[[], None]] = {}
        self._thread: threading.Thread | None = None

    def reserve(self) -> object:
        with self._condition:
            if len(self._reserved) >= self._limit:
                raise RegistrationError("Managed agent authority capacity exhausted")
            ticket = object()
            self._reserved.add(ticket)
            return ticket

    def release(self, ticket: object) -> None:
        with self._condition:
            self._reserved.discard(ticket)
            self._pending.pop(ticket, None)
            self._condition.notify_all()

    def defer(self, ticket: object, revoke: Callable[[], None]) -> None:
        with self._condition:
            if ticket not in self._reserved:
                raise RuntimeError("Authority cleanup was not reserved")
            self._pending[ticket] = revoke
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, daemon=True, name="yoke-agent-authority-release"
                )
                self._thread.start()

    def flush(self) -> None:
        """Try each pending release once, without holding the coordination lock."""
        with self._condition:
            pending = list(self._pending.items())
        for ticket, revoke in pending:
            try:
                revoke()
            except Exception:
                continue
            self.release(ticket)

    def wait_idle(self, timeout: float) -> bool:
        with self._condition:
            return self._condition.wait_for(
                lambda: not self._pending and self._thread is None, timeout
            )

    def _run(self) -> None:
        while True:
            with self._condition:
                if not self._pending:
                    self._thread = None
                    self._condition.notify_all()
                    return
                self._condition.wait(self._retry_seconds)
            self.flush()


REVOCATIONS = Revocations()
