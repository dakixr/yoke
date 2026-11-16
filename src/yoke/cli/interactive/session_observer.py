"""Render daemon-owned inputs without echoing locally rendered turns."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from threading import Thread

import httpx

from yoke.agent.models import Message
from yoke.cli.backend.daemon import DaemonSessionBackend
from yoke.cli.backend.events import BackendEvent, agent_event
from yoke.cli.backend.headless import input_output
from yoke.cli.runtime.base import EventRenderer

LOGGER = logging.getLogger(__name__)


class SessionObserver:
    """Own a session subscription for turns admitted outside the UI worker.

    Local input IDs are claimed before POST, so an early admission echo cannot
    win a rendering race. Daemon queue items are observed here too; the UI must
    never dequeue and re-admit them. Durable replay is already deduplicated by
    SessionConnection. Settlement IDs fence repeats across reconnects.
    """

    def __init__(
        self,
        backend: object,
        *,
        renderer: EventRenderer,
        user: Callable[[str], None],
        output: Callable[[str], None],
        notice: Callable[[str], None],
        messages: Callable[[list[Message]], None],
    ) -> None:
        self.backend = backend if isinstance(backend, DaemonSessionBackend) else None
        self.renderer = renderer
        self.user = user
        self.output = output
        self.notice = notice
        self.messages = messages
        self._prompts: dict[str, str] = {}
        self._rendered: set[str] = set()
        self._settled: set[str] = set()
        self._worker: Thread | None = None
        self._unsubscribe: Callable[[], None] = lambda: None

    def __enter__(self) -> SessionObserver:
        if self.backend is not None:
            stream, self._unsubscribe = self.backend.events.subscribe()
            self._worker = Thread(
                target=lambda: self._observe(stream),
                daemon=True,
                name="yoke-session-observer",
            )
            self._worker.start()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self._unsubscribe()
        if self._worker is not None:
            self._worker.join(timeout=10)

    def _observe(self, events: Iterable[BackendEvent]) -> None:
        backend = self.backend
        assert backend is not None
        for event in events:
            try:
                payload = event["payload"]
                input_id = payload.get("inputID")
                if (
                    not isinstance(input_id, str)
                    or backend.owns_input(input_id)
                    or input_id in self._settled
                ):
                    continue
                kind = event["type"]
                if kind == "session.prompt.admitted":
                    prompt = payload.get("prompt")
                    if isinstance(prompt, dict):
                        text = prompt.get("text")
                        if isinstance(text, str):
                            self._prompts[input_id] = text
                elif kind == "session.prompt.promoted":
                    self._begin(input_id)
                elif kind == "session.prompt.settled":
                    self._begin(input_id)
                    self._settled.add(input_id)
                    if payload.get("outcome") == "completed":
                        self.output(input_output(backend.connection, input_id))
                    else:
                        self.notice(
                            "Stopped current turn. Send a correction to continue from here."
                        )
                    self.renderer.__exit__(None, None, None)
                    self.messages(backend.messages())
                else:
                    self._begin(input_id)
                    self.renderer.handle_event(*agent_event(event))
            except (httpx.HTTPError, OSError, ValueError, RuntimeError) as exc:
                LOGGER.warning("Could not render daemon session event: %s", exc)
                self.notice(f"Could not refresh daemon output: {exc}")

    def _begin(self, input_id: str) -> None:
        if input_id in self._rendered:
            return
        self._rendered.add(input_id)
        prompt = self._prompts.pop(input_id, None)
        if prompt is not None:
            self.user(prompt)
        self.renderer.__enter__()
