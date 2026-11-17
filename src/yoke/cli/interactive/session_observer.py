"""Render daemon-owned inputs with one presentation lifetime per input."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from copy import copy
from threading import Thread

import httpx

from yoke.agent.models import Message
from yoke.cli.backend.daemon import DaemonSessionBackend
from yoke.cli.backend.events import BackendEvent, agent_event, AGENT_EVENTS
from yoke.cli.backend.headless import input_output
from yoke.cli.backend.values import EventRenderer

LOGGER = logging.getLogger(__name__)


class SessionObserver:
    """Observe queue promotions as well as inputs admitted by other terminals.

    The connection reduces authoritative phases before publishing each event.
    Render ownership is captured before terminal phases discard the live input.
    Each input ends its own renderer on settlement or a workspace pause.
    """

    def __init__(
        self,
        backend: DaemonSessionBackend,
        *,
        renderer: EventRenderer,
        user: Callable[[str], None],
        output: Callable[[str], None],
        notice: Callable[[str], None],
        messages: Callable[[list[Message]], None],
    ) -> None:
        self.backend = backend
        self.renderer = renderer
        self.user = user
        self.output = output
        self.notice = notice
        self.messages = messages
        self._prompts: dict[str, str] = {}
        self._renderers: dict[str, EventRenderer] = {}
        self._worker: Thread | None = None
        self._unsubscribe: Callable[[], None] = lambda: None

    def __enter__(self) -> SessionObserver:
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
        for renderer in self._renderers.values():
            renderer.__exit__(None, None, None)
        self._renderers.clear()

    def _observe(self, events: Iterable[BackendEvent]) -> None:
        for event in events:
            try:
                self._event(event)
            except (httpx.HTTPError, OSError, ValueError, RuntimeError) as exc:
                LOGGER.warning("Could not render daemon session event: %s", exc)
                self.notice(f"Could not refresh daemon output: {exc}")

    def _event(self, event: BackendEvent) -> None:
        payload, kind = event["payload"], event["type"]
        input_id = payload.get("inputID")
        if not isinstance(input_id, str) or payload.get(
            "renderedLocally", self.backend.owns_input(input_id)
        ):
            return
        if self.backend.view_store.input_lifecycle:
            if kind == "session.input.changed":
                phase = payload["phase"]
                if phase in {"queued", "paused", "promoted", "running"}:
                    self._prompts[input_id] = str(payload.get("prompt", ""))
                if phase in {"promoted", "running"}:
                    self._begin(input_id)
                elif phase in {"settled", "removed"}:
                    self._finish(input_id, payload.get("outcome"))
                elif phase == "paused":
                    self._end(input_id)
                return
            if kind not in AGENT_EVENTS:
                return
        elif kind == "session.prompt.admitted":
            prompt = payload.get("prompt")
            if isinstance(prompt, dict):
                self._prompts[input_id] = str(prompt.get("text", ""))
            return
        elif kind == "session.prompt.promoted":
            self._begin(input_id)
            return
        elif kind == "session.prompt.settled":
            self._finish(input_id, payload.get("outcome"))
            return
        elif kind not in AGENT_EVENTS:
            return
        renderer = self._renderers.get(input_id)
        if renderer is None:
            view = self.backend.view_store.view.inputs.get(input_id)
            if view is not None and view.phase in {"queued", "paused"}:
                return
            renderer = self._begin(input_id)
        renderer.handle_event(*agent_event(event))

    def _begin(self, input_id: str) -> EventRenderer:
        if input_id not in self._renderers:
            prompt = self._prompts.pop(input_id, None)
            if prompt is not None:
                self.user(prompt)
            renderer = copy(self.renderer)
            renderer.__enter__()
            self._renderers[input_id] = renderer
        return self._renderers[input_id]

    def _end(self, input_id: str) -> None:
        renderer = self._renderers.pop(input_id, None)
        if renderer is not None:
            renderer.__exit__(None, None, None)
        self._prompts.pop(input_id, None)

    def _finish(self, input_id: str, outcome: object) -> None:
        self._end(input_id)
        if outcome == "completed":
            self.output(input_output(self.backend.connection, input_id))
        elif outcome == "failed":
            self.notice("Current turn failed. Check the daemon error for details.")
        elif outcome in {"stopped", "recovered"}:
            self.notice(
                "Stopped current turn. Send a correction to continue from here."
            )
        if outcome is not None:
            self.messages(self.backend.messages())
