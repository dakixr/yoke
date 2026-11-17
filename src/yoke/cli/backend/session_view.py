"""A thread-safe, I/O-free projection of one daemon session."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from threading import RLock
from types import MappingProxyType
from typing import Literal

from yoke.cli.backend.events import BackendEvent
from yoke.http.models.prompt import InputInfo, QueueData, QueueHold, SessionInputs
from yoke.http.models.session import ActiveRuntimeInfo

LOGGER = logging.getLogger(__name__)
TERMINAL = {"settled", "removed"}


@dataclass(frozen=True, slots=True)
class InputView:
    id: str
    phase: str
    delivery: str
    position: int | None
    prompt: str
    origin: Literal["local", "remote"] = "remote"
    pause_cause: str | None = None
    turn_id: int | None = None


@dataclass(frozen=True, slots=True)
class RuntimeView:
    state: str = "idle"
    input_id: str | None = None
    turn_id: int | None = None
    operation: str | None = None
    revision: int = -1
    started_at: str | None = None
    activity: str | None = None


@dataclass(frozen=True, slots=True)
class SessionView:
    session_id: str
    inputs: Mapping[str, InputView] = field(
        default_factory=lambda: MappingProxyType({})
    )
    runtime: RuntimeView = field(default_factory=RuntimeView)
    held: str | None = None
    input_revision: int = 0
    connection: str = "discovering"
    resync_pending: bool = False

    @property
    def busy(self) -> bool:
        return self.runtime.state not in {"idle", "error"}

    @property
    def queue(self) -> list[InputView]:
        return sorted(
            (
                item
                for item in self.inputs.values()
                if item.phase in {"queued", "paused"}
            ),
            key=lambda item: (
                item.position if item.position is not None else 0,
                item.id,
            ),
        )

    @property
    def running(self) -> InputView | None:
        active = self.inputs.get(self.runtime.input_id or "")
        if active is not None and active.phase in {"promoted", "running"}:
            return active
        return next(
            (
                item
                for item in self.inputs.values()
                if item.phase in {"promoted", "running"}
            ),
            None,
        )


def _runtime(data: ActiveRuntimeInfo) -> RuntimeView:
    return RuntimeView(
        data.state,
        data.input_id,
        data.turn_id,
        data.operation.kind if data.operation else None,
        data.revision,
        data.started_at,
        data.activity,
    )


class SessionViewStore:
    """Own revisions and render ownership; subscribers receive immutable values.

    A revision gap latches resync_pending until a snapshot replaces the input
    projection. Terminal inputs and their local ownership are discarded.
    Callbacks run outside the store lock and must never perform I/O.
    """

    def __init__(
        self, session_id: str, owner: str, *, input_lifecycle: bool = True
    ) -> None:
        self.owner = owner
        self.input_lifecycle = input_lifecycle
        self._view = SessionView(session_id)
        self._local: set[str] = set()
        self._lock = RLock()
        self._callbacks: set[Callable[[SessionView], None]] = set()

    @property
    def view(self) -> SessionView:
        with self._lock:
            return self._view

    def select_session(self, session_id: str) -> None:
        with self._lock:
            self._local.clear()
            self._view = SessionView(session_id)
        self._notify()

    def mark_local(self, input_id: str) -> None:
        with self._lock:
            self._local.add(input_id)

    def forget_local(self, input_id: str) -> None:
        with self._lock:
            self._local.discard(input_id)

    def owns_input(self, input_id: str) -> bool:
        with self._lock:
            return input_id in self._local

    def _input(self, data: InputInfo) -> InputView:
        return InputView(
            data.id,
            data.phase,
            data.delivery,
            data.position,
            data.prompt,
            "local"
            if data.client_id == self.owner or data.id in self._local
            else "remote",
            data.pause_cause,
            data.turn_id,
        )

    def install_snapshot(
        self, inputs: Mapping[str, object], runtime: Mapping[str, object]
    ) -> SessionView:
        snapshot = SessionInputs.model_validate(inputs)
        active = ActiveRuntimeInfo.model_validate(runtime)
        with self._lock:
            # Snapshots are read on the stream owner before buffered SSE is reduced.
            live = {
                item.id: self._input(item)
                for item in snapshot.inputs
                if item.phase not in TERMINAL
            }
            self._local.intersection_update(live)
            self._view = replace(
                self._view,
                inputs=MappingProxyType(live),
                runtime=_runtime(active),
                held=snapshot.held.reason if snapshot.held else None,
                input_revision=snapshot.input_revision,
                resync_pending=False,
            )
        return self._notify()

    def install_legacy_snapshot(
        self, queue: Mapping[str, object], runtime: Mapping[str, object]
    ) -> SessionView:
        """Isolate the old daemon's started filter and unversioned queue snapshot."""
        snapshot = QueueData.model_validate(queue)
        active = ActiveRuntimeInfo.model_validate(runtime)
        with self._lock:
            live = {
                item.id: InputView(
                    item.id,
                    "paused" if item.paused else "queued",
                    item.delivery,
                    index,
                    item.prompt.text,
                    "local" if item.id in self._local else "remote",
                    item.pause_cause,
                )
                for index, item in enumerate(snapshot.items)
                if item.id not in self._local
            }
            self._view = replace(
                self._view, inputs=MappingProxyType(live), runtime=_runtime(active)
            )
        return self._notify()

    def apply_event(self, event: BackendEvent) -> SessionView:
        kind, payload = event["type"], event["payload"]
        with self._lock:
            view = self._view
            if kind == "client.connection":
                self._view = replace(view, connection=str(payload["phase"]))
            elif kind == "session.active.changed":
                active = ActiveRuntimeInfo.model_validate(payload)
                if active.revision <= view.runtime.revision:
                    return view
                self._view = replace(view, runtime=_runtime(active))
            elif (
                kind in {"session.input.changed", "session.queue.held"}
                and self.input_lifecycle
            ):
                revision = int(str(payload["inputRevision"]))
                if revision <= view.input_revision:
                    return view
                if view.resync_pending or revision != view.input_revision + 1:
                    self._view = replace(view, resync_pending=True)
                elif kind == "session.queue.held":
                    held = payload.get("held")
                    reason = (
                        QueueHold.model_validate(held).reason
                        if held is not None
                        else None
                    )
                    self._view = replace(view, input_revision=revision, held=reason)
                else:
                    item = InputInfo.model_validate(
                        {
                            key: value
                            for key, value in {
                                **payload,
                                "id": payload["inputID"],
                            }.items()
                            if key
                            in {
                                "id",
                                "phase",
                                "delivery",
                                "position",
                                "prompt",
                                "clientID",
                                "turnID",
                                "pauseCause",
                                "outcome",
                            }
                        }
                    )
                    values = dict(view.inputs)
                    if item.phase in TERMINAL:
                        values.pop(item.id, None)
                        self._local.discard(item.id)
                    else:
                        values[item.id] = self._input(item)
                    self._view = replace(
                        view, inputs=MappingProxyType(values), input_revision=revision
                    )
            elif not self.input_lifecycle:
                self._legacy_event(event)
            else:
                return view
            LOGGER.debug(
                "machine=cli_view id=%s event=%s revision=%s resync=%s",
                self._view.session_id,
                kind,
                self._view.input_revision,
                self._view.resync_pending,
            )
        return self._notify()

    def _legacy_event(self, event: BackendEvent) -> None:
        """Protocol-2 fallback only. Lifecycle-capable daemons never use inference."""
        kind, payload = event["type"], event["payload"]
        input_id = payload.get("inputID")
        if not isinstance(input_id, str):
            return
        values = dict(self._view.inputs)
        old = values.get(input_id)
        if kind in {"session.prompt.settled", "session.prompt.removed"}:
            values.pop(input_id, None)
            self._local.discard(input_id)
        elif kind in {"session.prompt.admitted", "session.prompt.promoted"}:
            prompt = payload.get("prompt")
            text = (
                str(prompt.get("text", ""))
                if isinstance(prompt, dict)
                else old.prompt
                if old
                else ""
            )
            values[input_id] = InputView(
                input_id,
                "promoted"
                if kind.endswith("promoted") or input_id in self._local
                else "queued",
                str(payload.get("delivery", "queue")),
                None,
                text,
                "local" if input_id in self._local else "remote",
            )
        self._view = replace(self._view, inputs=MappingProxyType(values))

    def subscribe(self, callback: Callable[[SessionView], None]) -> Callable[[], None]:
        with self._lock:
            self._callbacks.add(callback)
            callback(self._view)

        def unsubscribe() -> None:
            with self._lock:
                self._callbacks.discard(callback)

        return unsubscribe

    def _notify(self) -> SessionView:
        with self._lock:
            view, callbacks = self._view, tuple(self._callbacks)
        for callback in callbacks:
            callback(view)
        return view
