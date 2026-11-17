"""Prompt-toolkit turn-scoped renderer helpers."""

from __future__ import annotations

from collections.abc import Callable
from threading import Lock
from copy import copy

from yoke.cli.interactive.common import PromptCliState
from yoke.cli.interactive.renderer import PromptToolkitLiveRenderer


def make_turn_scoped_renderer_factory(
    *,
    state: PromptCliState,
    state_lock: Lock,
    renderer: PromptToolkitLiveRenderer,
) -> Callable[[int], PromptToolkitLiveRenderer]:
    """Wrap the shared renderer so abandoned turns stop rendering."""

    class TurnScopedRenderer(PromptToolkitLiveRenderer):
        def __init__(self, turn_id: int) -> None:
            self.turn_id = turn_id
            self.delegate = copy(renderer)

        def _active(self) -> bool:
            with state_lock:
                abandoned_turn_ids, _ = (
                    state.abandoned_turn_ids,
                    state.steered_turn_ids,
                )
                active_turn_id = state.active_turn_id
                input_id = state.rendering_inputs.get(self.turn_id)
                if state.view is not None and input_id is not None:
                    return input_id not in state.fenced_inputs
            return (
                self.turn_id == active_turn_id
                and self.turn_id not in abandoned_turn_ids
            )

        def __enter__(self) -> PromptToolkitLiveRenderer:
            if self._active():
                self.delegate.__enter__()
            return self

        def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
            if self._active():
                self.delegate.__exit__(_exc_type, _exc, _tb)

        def handle_event(self, event: str, payload: dict[str, object]) -> None:
            if self._active():
                turn_payload = dict(payload)
                turn_payload["turn_id"] = payload.get("turnID", self.turn_id)
                self.delegate.handle_event(event, turn_payload)

    return TurnScopedRenderer
