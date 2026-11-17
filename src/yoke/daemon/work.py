"""Explain daemon liveness through runtime phases and durable queue work."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

from yoke.session.queue import has_queued_prompts, queue_work_sessions


if TYPE_CHECKING:
    from yoke.http.runtime.registry import SessionRuntimeRegistry
    from yoke.session import SessionStore


@dataclass(frozen=True, slots=True)
class WorkSummary:
    """Held queues are informational; every other reason blocks an idle exit."""

    running_sessions: tuple[str, ...] = ()
    runnable_queues: tuple[str, ...] = ()
    held_queues: tuple[str, ...] = ()
    mutations: int = 0
    admission_changed: bool = False

    @property
    def busy(self) -> bool:
        return bool(
            self.running_sessions
            or self.runnable_queues
            or self.mutations
            or self.admission_changed
        )

    def __bool__(self) -> bool:
        return self.busy


class DaemonWork:
    """Read work through public runtime and storage interfaces."""

    def __init__(self, store: SessionStore, registry: SessionRuntimeRegistry) -> None:
        self.store = store
        self.registry = registry

    async def busy(self) -> WorkSummary:
        from yoke.http.runtime.phase import Idle

        running = tuple(
            session_id
            for session_id, runtime in self.registry.loaded_runtimes()
            if runtime.workers.active_count
            or not isinstance(runtime.activity.phase, Idle)
        )
        runnable, held = await asyncio.to_thread(
            queue_work_sessions, self.store.directory
        )
        return WorkSummary(
            running_sessions=running, runnable_queues=runnable, held_queues=held
        )

    def queued(self) -> bool:
        return has_queued_prompts(self.store.directory)
