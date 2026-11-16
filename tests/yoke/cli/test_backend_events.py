"""Daemon SSE events feed the terminal vocabulary and each subscriber."""

import json
from itertools import takewhile
from threading import Event

import pytest

from yoke.cli.backend.events import (
    EventStream,
    EventSubscriptions,
    PUBLIC_AGENT_EVENTS,
    agent_event,
    event_from_sse,
)
from yoke.cli.backend.models import TurnSuccess
from yoke.cli.interactive.turn_stream import render_turn_events
from yoke.cli.render import InteractiveRenderer

from .terminal_daemon_support import terminal as terminal
from .test_turn_projection import RecordingProvider
from yoke.agent.loop.agent import RuntimeAgent


def test_subscription_preserves_order_and_closes_after_queued_events():
    subscriptions = EventSubscriptions()
    stream, unsubscribe = subscriptions.subscribe()
    for number in range(25):
        subscriptions.publish("session.notice", {"number": number})
    unsubscribe()
    subscriptions.publish("session.notice", {"number": 99})
    assert list(stream) == [
        {"type": "session.notice", "payload": {"number": number}}
        for number in range(25)
    ]
    unsubscribe()


@pytest.mark.parametrize("runtime_type,public_type", PUBLIC_AGENT_EVENTS.items())
def test_sse_envelopes_keep_every_rendered_payload_field(runtime_type, public_type):
    payload = {"content": "text\n  whitespace", "iteration": 2, "usage": {"tokens": 4}}
    envelope = json.loads(json.dumps({"type": public_type, "data": payload}))
    assert agent_event(event_from_sse(envelope)) == (runtime_type, payload)


def test_invalid_sse_envelopes_fail_explicitly():
    for envelope in (
        {"type": "notice"},
        {"type": 3, "data": {}},
        {"type": "x", "data": {1: "x"}},
    ):
        with pytest.raises(ValueError):
            event_from_sse(envelope)


def test_streamed_turn_fans_out_to_session_subscribers(tmp_path, terminal):
    import io

    terminal.factory = lambda _record: RuntimeAgent(
        RecordingProvider(), [], tool_root=tmp_path
    )
    terminal.store.save("events", [], root=tmp_path, title="Named fixture")
    backend = terminal.backend("events", root=tmp_path)
    stream, unsubscribe = backend.events.subscribe()
    submitted = backend.submit(
        "hello",
        [],
        entries=[],
        stop_event=Event(),
        user_message=None,
        checkpoint=lambda *_args: None,
    )
    events = list(submitted)
    outcome = submitted.result()
    try:
        assert isinstance(outcome, TurnSuccess)
        unsubscribe()
        lifecycle = {
            "session.prompt.admitted",
            "session.prompt.promoted",
            "session.prompt.settled",
        }
        assert [
            event
            for event in takewhile(
                lambda item: item["type"] != "session.prompt.settled", stream
            )
            if event["payload"].get("inputID") == submitted.input_id
            and event["type"] not in lifecycle
        ] == events
        assert {"session.model.started", "session.model.ended"} <= {
            event["type"] for event in events
        }
        assert all(set(event) == {"type", "payload"} for event in events)
        assert json.loads(json.dumps(events)) == events
        render_turn_events(events, InteractiveRenderer(io.StringIO()))
    finally:
        unsubscribe()
        backend.retire_turn(outcome.agent)


def test_closed_stream_does_not_accept_more_events():
    stream = EventStream()
    stream.close()
    stream.publish("notice", {"content": "late"})
    assert list(stream) == []
