"""Client leases and the daemon's exit decision.

A lease is an open event stream from a client that names itself. The daemon
stays up while it has a lease or a turn is running or stopping; once neither
holds for ``grace_seconds`` it exits. ``idle_exit=False`` (``--no-idle-exit``)
disables that, for a daemon a service manager keeps running.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

DEFAULT_GRACE_SECONDS = 5.0

type BusyProbe = Callable[[], Awaitable[bool]]
type ExitCallback = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class Lease:
    id: str
    client: str
    pid: int | None
    opened_at: float


class DaemonLifecycle:
    """Track leases, pending restarts and the idle grace period."""

    def __init__(
        self,
        *,
        idle_exit: bool = True,
        grace_seconds: float = DEFAULT_GRACE_SECONDS,
        poll_seconds: float = 0.2,
    ) -> None:
        self.idle_exit = idle_exit
        self.grace_seconds = grace_seconds
        self.poll_seconds = poll_seconds
        self.restart_pending = False
        self.exit_reason: str | None = None
        self._leases: dict[str, Lease] = {}
        self._busy: BusyProbe | None = None
        self._on_exit: ExitCallback | None = None
        self._exiting: asyncio.Event | None = None
        self._mutations = 0

    def begin_mutation(self) -> None:
        """Keep an admitted HTTP mutation alive until its handler finishes."""
        self._mutations += 1

    def end_mutation(self) -> None:
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
        return lease

    def close_lease(self, lease: Lease) -> None:
        self._leases.pop(lease.id, None)

    def leases(self) -> list[Lease]:
        return list(self._leases.values())

    async def busy(self) -> bool:
        active = await self._busy() if self._busy is not None else False
        return active or self._mutations > 0

    def request_exit(self, reason: str) -> None:
        """Begin shutdown once; later requests keep the first reason."""
        if self.exit_reason is not None:
            return
        self.exit_reason = reason
        self.exiting.set()
        if self._on_exit is not None:
            self._on_exit(reason)

    def request_restart_when_idle(self) -> None:
        self.restart_pending = True

    async def watch(self) -> None:
        """Exit after the grace period with no leases and no turns."""
        idle_since: float | None = None
        while self.exit_reason is None:
            await asyncio.sleep(self.poll_seconds)
            busy = await self.busy()
            if self.restart_pending and not busy:
                self.request_exit("restart")
                return
            if not self.idle_exit or busy or self._leases:
                idle_since = None
                continue
            now = time.monotonic()
            if idle_since is None:
                idle_since = now
            elif now - idle_since >= self.grace_seconds:
                self.request_exit("idle")
                return
