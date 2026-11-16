"""An HTTP 409 must refresh the UI manager's authoritative queue."""

from threading import Lock

from yoke.cli.backend.connection import DaemonRequestError
from yoke.cli.interactive.common import PromptCliState
from yoke.cli.interactive.queue.mutations import replace_prompt_queue

from .interactive_daemon_support import (
    cast_session,
    interactive_daemon as interactive_daemon,
)


def test_daemon_queue_http_409_refreshes_without_losing_other_clients_edit(
    interactive_daemon, monkeypatch
):
    d = interactive_daemon
    d.connection.session_request(
        "POST",
        "/prompt",
        json={"id": "first", "resume": False, "prompt": {"text": "original"}},
    )
    loaded = d.backend.load_queue()
    state = PromptCliState([], loaded.prompts, queue_revision=loaded.revision)
    edited = [item.copy_for_queue() for item in loaded.prompts]
    edited[0].prompt = "stale manager edit"
    request = d.connection.session_request
    conflicts = []

    def race(method, suffix="", **kwargs):
        if method == "PATCH" and suffix == "/queue":
            request(
                "POST",
                "/prompt",
                json={
                    "id": "second",
                    "resume": False,
                    "prompt": {"text": "concurrent addition"},
                },
            )
        try:
            return request(method, suffix, **kwargs)
        except DaemonRequestError as exc:
            conflicts.append(exc.status)
            raise

    monkeypatch.setattr(d.connection, "session_request", race)
    assert not replace_prompt_queue(
        state=state,
        state_lock=Lock(),
        active_session=cast_session(d.backend),
        prompts=edited,
        expected_revision=loaded.revision,
    )
    assert conflicts == [409]
    assert [item.prompt for item in state.pending_prompts] == [
        "original",
        "concurrent addition",
    ]
    assert state.queue_revision == loaded.revision + 1
    assert len(d.tape.calls) == 0
