"""Automatic session-title generation for HTTP-owned runtimes."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import Executor
from contextlib import suppress
import logging
from threading import Lock

from yoke.agent.models import Message
from yoke.http.services.event_broker import EventService
from yoke.http.services.runtime_factory import SessionAgentFactory
from yoke.http.services.runtime_factory import generate_http_session_title
from yoke.http.services.runtime_persistence import input_is_persisted
from yoke.http.services.session_read_cache import SessionReadCache
from yoke.session import SessionRecord
from yoke.session import SessionStore
from yoke.session import fallback_session_title
from yoke.session.admissions import AdmissionRecord


LOGGER = logging.getLogger(__name__)
type UserMessageFactory = Callable[[SessionRecord, AdmissionRecord], Message]


class SessionTitleAutomation:
    """Generate one missing title alongside the first active prompt."""

    def __init__(
        self,
        session_id: str,
        *,
        store: SessionStore,
        events: EventService,
        read_cache: SessionReadCache,
        agent_factory: SessionAgentFactory,
        executor: Executor,
        persistence_lock: Lock,
        user_message_factory: UserMessageFactory,
    ) -> None:
        self.session_id = session_id
        self.store = store
        self.events = events
        self.read_cache = read_cache
        self.agent_factory = agent_factory
        self.executor = executor
        self.persistence_lock = persistence_lock
        self.user_message_factory = user_message_factory
        self._task: asyncio.Task[None] | None = None

    def start(self, admission: AdmissionRecord) -> None:
        """Start title generation when this session is still unnamed."""
        current_task = self._task
        if current_task is not None and not current_task.done():
            return
        summary = self.store.summary_record(self.session_id)
        if summary is None or summary.title:
            return
        self._task = asyncio.create_task(
            self._generate(admission),
            name=f"yoke-http-title-{self.session_id}",
        )

    def cancel(self) -> None:
        """Prevent an in-flight generated title from replacing an explicit edit."""
        task = self._task
        if task is not None and not task.done():
            task.cancel()
        self._task = None

    async def close(self) -> None:
        """Let an in-flight title finish within the registry shutdown grace period."""
        task = self._task
        if task is None:
            return
        with suppress(asyncio.CancelledError):
            await asyncio.shield(task)

    async def _generate(self, admission: AdmissionRecord) -> None:
        generated: str | None = None
        try:
            loop = asyncio.get_running_loop()
            generated = await loop.run_in_executor(
                self.executor,
                self._generate_sync,
                admission,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Automatic session title generation failed: %s", exc)

        title = generated or fallback_session_title(admission.prompt)
        with self.persistence_lock:
            current = self.store.summary_record(self.session_id)
            if current is None or current.title:
                return
            record = self.store.set_title(
                self.session_id,
                title,
                existing_record=current,
            )
        self.events.durable(
            self.session_id,
            "session.updated",
            {
                "sessionID": record.id,
                "title": record.title,
                "pinned": record.pinned,
                "archivedAt": record.archived_at,
            },
            location=record.root,
        )

    def _generate_sync(self, admission: AdmissionRecord) -> str | None:
        snapshot = self.read_cache.get(self.session_id)
        record = snapshot.record
        messages = list(record.messages)
        if not input_is_persisted(record, admission.id):
            messages.append(self.user_message_factory(record, admission))
        return generate_http_session_title(
            self.agent_factory,
            record,
            messages,
        )
