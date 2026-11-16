"""Consume backend event dictionaries using the existing terminal renderers."""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import ExitStack

from yoke.cli.backend.events import BackendEvent, agent_event
from yoke.cli.runtime.base import EventRenderer


def render_turn_events(events: Iterable[BackendEvent], renderer: EventRenderer) -> None:
    """Render a streamed turn; rejected admissions never enter the renderer."""
    with ExitStack() as lifetime:
        entered = False
        for event in events:
            if not entered:
                lifetime.enter_context(renderer)
                entered = True
            renderer.handle_event(*agent_event(event))
