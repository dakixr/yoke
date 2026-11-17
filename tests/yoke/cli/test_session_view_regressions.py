"""CLI lifecycle regressions against an isolated daemon."""

import json
from pathlib import Path
from yoke.agent.models import Message
from threading import Lock, Event

from yoke.agent.loop import RuntimeAgent
from yoke.cli.backend.models import PendingPrompt
from yoke.cli.interactive.common import PromptCliState
from yoke.cli.interactive.queue.mutations import queue_prompt_submission
from yoke.cli.interactive.prompt.rendering import build_prompt_toolbar

from .terminal_daemon_support import terminal as terminal
from .test_started_turn_queue_view import HeldProvider


def test_tab_racing_idle_resumes_instead_of_stranding(terminal, tmp_path):
    backend = terminal.backend("tab-idle", root=tmp_path)
    state = PromptCliState(
        messages=[], pending_prompts=[], view=backend.view_store.view
    )
    assert queue_prompt_submission(
        state=state,
        state_lock=Lock(),
        active_session=backend.session,
        prompt=PendingPrompt("tab at idle"),
        consumed_images=[],
    )
    backend.connection.session_request("POST", "/wait", params={"timeoutMs": 5000})
    assert backend.connection.session_request("GET", "/queue")["data"]["items"] == []
    assert "synthetic response" in str(backend.messages())


def test_queued_turn_drives_toolbar_and_precise_stop(terminal, tmp_path):
    provider = HeldProvider()
    terminal.factory = lambda record: RuntimeAgent(
        provider, [], tool_root=Path(record.root)
    )
    backend = terminal.backend("queue-active", root=tmp_path)
    try:
        loaded = backend.load_queue()
        backend.commit_queue(
            [PendingPrompt("queued")], [], expected_revision=loaded.revision
        )
        backend.connection.session_request("POST", "/queue/resume")
        assert provider.entered.wait(5)
        # A fresh attachment has no local rendering thread, but must show busy.
        other = terminal.backend("queue-active", root=tmp_path)
        state = PromptCliState(
            messages=[], pending_prompts=[], view=other.connection.view_store.view
        )
        assert state.view is not None
        assert state.worker is None and state.view.busy
        toolbar = build_prompt_toolbar(
            state=state,
            state_lock=Lock(),
            provider_model_text=None,
            spinner_frames=("SPINNER",),
            root_label="root",
        )()
        assert "SPINNER" in "".join(text for _, text in toolbar)
        assert other.stop_running()
        assert other.connection.session_request("GET", "/input")["data"]["inputs"] == []
    finally:
        provider.release.set()


def test_workspace_paused_input_is_visible_in_capability_queue(terminal, tmp_path):
    root = tmp_path / "gone"
    root.mkdir()
    backend = terminal.backend("workspace-paused", root=root)
    from yoke.session.workspace import WorkspaceUnavailable

    def unavailable(record):
        root.rmdir()
        raise WorkspaceUnavailable(root, session_id=record.id)

    terminal.factory = unavailable
    submitted = backend.submit("keep me visible", user_message=None)
    list(submitted)
    backend.view_store.mark_local(submitted.input_id)
    loaded = backend.load_queue()
    assert [item.id for item in loaded.prompts] == [submitted.input_id]
    assert loaded.prompts[0].paused


def test_cli_steer_uses_daemon_delivery_without_interrupt(
    terminal, tmp_path, monkeypatch
):
    from unittest.mock import Mock
    from yoke.cli.backend.values import ActiveSessionRef
    from yoke.cli.backend.session_view import SessionViewStore
    from yoke.cli.interactive.prompt.control import create_prompt_toolkit_control

    backend = terminal.backend("steer-delivery", root=tmp_path)
    store = SessionViewStore("steer-delivery", backend.connection.owner)
    store.install_snapshot(
        {"inputRevision": 0, "inputs": []}, {"state": "running", "revision": 1}
    )
    state = PromptCliState(messages=[], pending_prompts=[], view=store.view)
    deliveries = []
    completed = Event()

    def run(**kwargs):
        deliveries.append(kwargs.get("delivery", "queue"))
        completed.set()

    monkeypatch.setattr("yoke.cli.interactive.prompt.control.run_prompt_turn", run)
    monkeypatch.setattr(
        backend,
        "interrupt",
        Mock(side_effect=AssertionError("steering interrupted separately")),
    )
    control = create_prompt_toolkit_control(
        state=state,
        agent=backend,
        active_session_ref=ActiveSessionRef(backend.session),
        renderer=Mock(),
        state_lock=Lock(),
        request_context_usage=lambda _: None,
        invalidate_prompt=lambda: None,
        update_status=lambda _: None,
        scrollback=Mock(),
    )
    assert control.steer_active_turn("correct this", None)
    assert completed.wait(2)
    assert deliveries == ["steer"]


def test_queue_http_never_holds_the_toolbar_lock(terminal, tmp_path, monkeypatch):
    backend = terminal.backend("queue-lock", root=tmp_path)
    state = PromptCliState(
        messages=[], pending_prompts=[], view=backend.view_store.view
    )
    lock = Lock()
    request = backend.connection.session_request
    seen = []

    def checked(method, suffix="", **kwargs):
        assert not lock.locked(), (method, suffix)
        seen.append((method, suffix))
        return request(method, suffix, **kwargs)

    monkeypatch.setattr(backend.connection, "session_request", checked)
    assert queue_prompt_submission(
        state=state,
        state_lock=lock,
        active_session=backend.session,
        prompt=PendingPrompt("paused", paused=True),
        consumed_images=[],
    )
    assert ("PATCH", "/queue") in seen


def test_steer_overtakes_an_earlier_ordinary_queue_item(terminal, tmp_path):
    from unittest.mock import MagicMock
    from yoke.cli.backend.values import ActiveSessionRef
    from yoke.cli.interactive.prompt.control import create_prompt_toolkit_control
    from .test_steering_differential import SteeringProvider

    provider = SteeringProvider()
    provider.responses = iter(
        [
            Message.assistant("discarded"),
            Message.assistant("correction done"),
            Message.assistant("backlog done"),
        ]
    )
    terminal.factory = lambda record: RuntimeAgent(
        provider, [], tool_root=Path(record.root)
    )
    backend = terminal.backend("steer-order", root=tmp_path)
    backend.connection.session_request("PATCH", json={"title": "fixed"})
    backend.submit("original", user_message=None)
    assert provider.entered.wait(5)
    loaded = backend.load_queue()
    backend.commit_queue(
        [PendingPrompt("backlog")], [], expected_revision=loaded.revision
    )
    state = PromptCliState(
        messages=[], pending_prompts=[], view=backend.view_store.view
    )
    control = create_prompt_toolkit_control(
        state=state,
        agent=backend,
        active_session_ref=ActiveSessionRef(backend.session),
        renderer=MagicMock(),
        state_lock=Lock(),
        request_context_usage=lambda _: None,
        invalidate_prompt=lambda: None,
        update_status=lambda _: None,
        scrollback=MagicMock(),
    )
    assert control.steer_active_turn("correction", None)
    worker = state.worker
    assert worker is not None
    worker.join(5)
    assert not worker.is_alive()
    backend.connection.session_request("POST", "/wait", params={"timeoutMs": 5000})
    requests = [json.loads(str(record["messages_json"])) for record in provider.records]
    assert [request[-1]["content"] for request in requests] == [
        "original",
        "correction",
        "backlog",
    ]


def test_stop_does_not_fence_a_different_local_input_waiting_in_queue(
    terminal, tmp_path, monkeypatch
):
    from threading import Thread
    from unittest.mock import MagicMock
    from yoke.cli.backend.values import ActiveSessionRef
    from yoke.cli.backend.session_view import SessionViewStore
    from yoke.cli.interactive.prompt.control import create_prompt_toolkit_control

    backend = terminal.backend("stop-identity", root=tmp_path)
    store = SessionViewStore("stop-identity", backend.connection.owner)
    store.install_snapshot(
        {
            "inputRevision": 0,
            "inputs": [
                {
                    "id": "remote",
                    "phase": "running",
                    "prompt": "running",
                    "delivery": "queue",
                },
                {
                    "id": "local",
                    "phase": "queued",
                    "prompt": "waiting",
                    "delivery": "queue",
                    "position": 0,
                },
            ],
        },
        {"state": "running", "revision": 1, "inputID": "remote"},
    )
    stop_event = Event()
    state = PromptCliState(
        messages=[],
        pending_prompts=[],
        view=store.view,
        active_turn_id=1,
        active_stop_request=stop_event,
        worker=Thread(),
    )
    state.rendering_inputs[1] = "local"
    targeted, finished = [], Event()

    def stop(input_id):
        targeted.append(input_id)
        finished.set()
        return True

    monkeypatch.setattr(backend, "stop_input", stop)
    control = create_prompt_toolkit_control(
        state=state,
        agent=backend,
        active_session_ref=ActiveSessionRef(backend.session),
        renderer=MagicMock(),
        state_lock=Lock(),
        request_context_usage=lambda _: None,
        invalidate_prompt=lambda: None,
        update_status=lambda _: None,
        scrollback=MagicMock(),
    )
    assert control.stop_active_turn()
    assert finished.wait(2) and targeted == ["remote"]
    assert not stop_event.is_set()
    assert not state.fenced_inputs
    assert state.worker is not None
