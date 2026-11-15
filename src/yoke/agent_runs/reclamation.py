"""Bounded ownership reservations and retries for remote authority release."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
import threading
import time

from yoke.agent_runs.protocol import RegistrationError

logger = logging.getLogger(__name__)


@dataclass
class _Retry:
    revoke: Callable[[], None]
    attempts: int
    next_attempt: float


class Revocations:
    """Reserve cleanup capacity before acquisition, retain it until release ACK.

    Exhausted retries keep their reservation so an outage cannot cause unbounded
    authority leaks. Explicit release can still reclaim that capacity.
    """

    def __init__(
        self, *, limit: int = 4096, retry_seconds: float = 1, max_attempts: int = 8
    ) -> None:
        if retry_seconds <= 0 or max_attempts < 1:
            raise ValueError("Retry interval and attempt limit must be positive")
        self._limit = limit
        self._retry_seconds = retry_seconds
        self._max_attempts = max_attempts
        self._condition = threading.Condition()
        self._reserved: set[object] = set()
        self._pending: dict[object, _Retry] = {}
        self._thread: threading.Thread | None = None
        self._flush_lock = threading.Lock()

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
            self._pending[ticket] = _Retry(
                revoke, 0, time.monotonic() + self._retry_seconds
            )
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, daemon=True, name="yoke-agent-authority-release"
                )
                self._thread.start()
            self._condition.notify_all()

    def flush(self, *, due_only: bool = False) -> None:
        """Try pending releases once; automatic retries respect their backoff."""
        with self._flush_lock:
            with self._condition:
                pending = list(self._pending.items())
            for ticket, retry in pending:
                if due_only and retry.next_attempt > time.monotonic():
                    continue
                try:
                    retry.revoke()
                except Exception:
                    retry.attempts += 1
                    exhausted = retry.attempts >= self._max_attempts
                    logger.warning(
                        "Agent authority release failed on attempt %s/%s%s",
                        retry.attempts,
                        self._max_attempts,
                        "; retries exhausted, retaining capacity reservation"
                        if exhausted
                        else "",
                        exc_info=True,
                    )
                    with self._condition:
                        if self._pending.get(ticket) is retry:
                            if exhausted:
                                self._pending.pop(ticket)
                            else:
                                retry.next_attempt = time.monotonic() + min(
                                    60,
                                    self._retry_seconds * 2 ** min(retry.attempts, 16),
                                )
                        self._condition.notify_all()
                else:
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
                delay = (
                    min(retry.next_attempt for retry in self._pending.values())
                    - time.monotonic()
                )
                if delay > 0:
                    self._condition.wait(delay)
                    continue
            self.flush(due_only=True)


REVOCATIONS = Revocations()
