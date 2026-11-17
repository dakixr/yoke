"""Daemon liveness includes physical workers and durable queued inputs."""

from __future__ import annotations

import asyncio

from yoke.http.runtime.registry import SessionRuntimeRegistry
from yoke.session import SessionStore
from yoke.session.queue import has_queued_prompts


class DaemonWork:
    """Read work through public runtime and storage interfaces."""

    def __init__(self, store: SessionStore, registry: SessionRuntimeRegistry) -> None:
        self.store = store
        self.registry = registry

    async def busy(self) -> bool:
        for _session_id, runtime in self.registry.loaded_runtimes():
            if runtime.workers.active_count:
                return True
            status = await runtime.status()
            if status.state in {"running", "stopping", "waiting_input"}:
                return True
        return await asyncio.to_thread(self.queued)

    def queued(self) -> bool:
        # The lifecycle polls this several times a second; one indexed EXISTS
        # keeps an idle daemon idle however many sessions are stored.
        return has_queued_prompts(self.store.directory)
