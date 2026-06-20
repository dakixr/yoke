"""Prompt-toolkit turn-scoped renderer helpers."""

from __future__ import annotations

from collections.abc import Callable
from threading import Lock

from yoke.cli.interactive.common import PromptCliState
from yoke.cli.interactive.common import prompt_turn_tracking
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

        def _active(self) -> bool:
            with state_lock:
                abandoned_turn_ids, _ = prompt_turn_tracking(state)
            return self.turn_id not in abandoned_turn_ids

        def __enter__(self) -> PromptToolkitLiveRenderer:
            if self._active():
                renderer.__enter__()
            return self

        def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
            if self._active():
                renderer.__exit__(_exc_type, _exc, _tb)

        def handle_event(self, event: str, payload: dict[str, object]) -> None:
            if self._active():
                renderer.handle_event(event, payload)

        def print_agent_output(self, text: str) -> None:
            if self._active():
                renderer.print_agent_output(text)

        def print_error(self, message: str) -> None:
            if self._active():
                renderer.print_error(message)

        def _emit_turn_summary(self, summary: dict[str, object]) -> None:
            if self._active():
                emit = getattr(renderer, "_emit_turn_summary", None)
                if callable(emit):
                    emit(summary)

    return TurnScopedRenderer
