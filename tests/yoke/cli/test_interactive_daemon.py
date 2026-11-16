"""Interactive daemon regressions, exercised through both UI implementations."""

from __future__ import annotations

import io
from threading import Lock

import pytest

from tests.yoke.provider_projection.recording import Gate, answer, tool_calls
from yoke.cli.backend import SessionBackend
from yoke.cli.interactive.common import PromptCliState
from yoke.cli.interactive.queue.mutations import replace_prompt_queue
from yoke.cli.interactive.session_commands import print_session_info
from yoke.cli.interactive.tool_inspector.render import detail_text
from yoke.cli.interactive.tool_inspector.state import ToolInspectorState
from yoke.cli.interactive.tree_selector import TreeSelectorResult
from yoke.cli.render import build_console
from yoke.cli.runtime import ActiveSession
from yoke.session import SessionStore
from yoke.http.models.tool_trace import ToolCallInfo

from .interactive_daemon_support import (
    interactive_daemon as interactive_daemon,
    cast_session,
)


@pytest.mark.parametrize("mode", ["basic", "prompt"])
def test_daemon_tool_turn_renders_prompt_call_and_answer_once(
    interactive_daemon, monkeypatch, mode
):
    d = interactive_daemon
    d.tape.expect(tool_calls(("read", {"path": "input.txt"})))
    d.tape.expect(answer("unique tool answer"))

    def script(ui):
        ui.send("unique tool prompt")
        ui.wait_text("unique tool answer")
        ui.idle()
        # A later command runs after the completion/queue handoff too.
        ui.send("/info")
        ui.wait_text("Session info:")

    text = d.run(mode, script, monkeypatch)
    assert text.count("unique tool prompt") == 1
    assert text.count("read path=") == 1
    assert text.count("unique tool answer") == 1
    assert len(d.tape.calls) == 2
    assert not d.tape.steps


@pytest.mark.parametrize("mode", ["basic", "prompt"])
def test_daemon_second_client_prompt_renders_once(
    interactive_daemon, monkeypatch, mode
):
    d = interactive_daemon
    d.tape.expect(answer("other client answer"))

    def script(ui):
        # Wait for the observer to be installed by submitting a local command.
        ui.send("/info")
        ui.wait_text("Session info:")
        ui.connection.session_request(
            "PUT",
            "/client",
            json={
                "owner": "second-client",
                "kind": "cli",
                "mode": "full-access",
                "cwd": str(ui.world.root),
                "env": {},
            },
        )
        ui.connection.session_request(
            "POST",
            "/prompt",
            json={
                "id": "other-input",
                "clientID": "second-client",
                "prompt": {"text": "other client prompt"},
            },
        )
        ui.wait_text("other client answer")
        ui.idle()

    text = d.run(mode, script, monkeypatch)
    assert text.count("other client prompt") == 1
    assert text.count("other client answer") == 1
    assert len(d.tape.calls) == 1


def test_daemon_info_matches_every_in_process_line(interactive_daemon, monkeypatch):
    d = interactive_daemon
    d.tape.expect(answer("metadata answer"))

    def script(ui):
        ui.send("metadata prompt")
        ui.wait_text("metadata answer")
        ui.idle()

    d.run("basic", script, monkeypatch)
    record = SessionStore(d.world.directory).load(d.connection.session_id)
    assert record.created_at != record.updated_at
    assert record.context_window_tokens == 100_000
    local = ActiveSession(
        record.id, d.world.root, SessionStore(d.world.directory), record
    )
    expected, actual = io.StringIO(), io.StringIO()
    print_session_info(
        build_console(expected), local, backend=SessionBackend(object(), local)
    )
    print_session_info(
        build_console(actual),
        cast_session(d.backend),
        backend=SessionBackend.wrap(d.backend),
    )
    assert actual.getvalue() == expected.getvalue()
    assert f"Database: {d.world.directory / 'yoke.db'}" in actual.getvalue()
    assert "Conversation entries: 0" not in actual.getvalue()
    assert "Context window: 100000 tokens" in actual.getvalue()


def test_daemon_queue_conflict_refreshes_manager_without_overwriting(
    interactive_daemon,
):
    d = interactive_daemon
    # Dormant admissions let a second client change the revision while the UI
    # manager holds a snapshot. Neither input is allowed to execute here.
    for ident in ("first",):
        d.connection.session_request(
            "POST",
            "/prompt",
            json={"id": ident, "resume": False, "prompt": {"text": ident}},
        )
    loaded = d.backend.load_queue()
    state = PromptCliState([], loaded.prompts, queue_revision=loaded.revision)
    d.connection.session_request(
        "POST",
        "/prompt",
        json={"id": "second", "resume": False, "prompt": {"text": "second"}},
    )
    assert not replace_prompt_queue(
        state=state,
        state_lock=Lock(),
        active_session=cast_session(d.backend),
        prompts=[],
        expected_revision=loaded.revision,
    )
    assert [item.prompt for item in state.pending_prompts] == ["first", "second"]
    assert state.queue_revision > loaded.revision
    assert [item.prompt for item in d.backend.load_queue().prompts] == [
        "first",
        "second",
    ]


def test_daemon_tool_inspector_reads_typed_call_and_result(
    interactive_daemon, monkeypatch
):
    d = interactive_daemon
    d.tape.expect(tool_calls(("read", {"path": "input.txt"})))
    d.tape.expect(answer("inspection done"))
    d.run(
        "prompt",
        lambda ui: (
            ui.send("inspect tool"),
            ui.wait_text("inspection done"),
            ui.idle(),
        ),
        monkeypatch,
    )
    inspection = d.backend.tool_inspection()
    entries = inspection.snapshots()
    assert len(entries) == 1
    call = inspection.call(entries[0].tool_call_id)
    assert call is not None
    assert call.tool_name == "read"
    calls = [call for message in d.backend.messages() for call in message.tool_calls]
    assert len(calls) == 1
    assert calls[0].id == call.tool_call_id
    assert calls[0].function.name == "read"
    assert call.raw_arguments == '{"path":"input.txt"}'
    assert call.status == "ok"
    raw = d.connection.session_request("GET", f"/tool-call/{call.tool_call_id}")["data"]
    assert call.duration_seconds == pytest.approx(raw["time"]["durationMs"] / 1000)
    raw["time"]["durationMs"] = 0
    instant = inspection.present(ToolCallInfo.model_validate(raw))
    assert instant.duration_seconds == 0.0
    assert call.result is not None
    assert "alpha" in str(call.result)
    assert "input.txt" in detail_text(call, ToolInspectorState(entries=[call]))


def test_daemon_prompt_steer_during_tool_checkpoint(interactive_daemon, monkeypatch):
    d = interactive_daemon
    gate = Gate()
    d.tape.expect(tool_calls(("read", {"path": "input.txt"})))
    d.tape.expect(answer("abandoned output"), gate=gate)
    d.tape.expect(answer("steered answer"))

    def script(ui):
        ui.send("read then wait")
        assert gate.entered.wait(5)
        ui.send("steer correction")
        try:
            ui.wait_text("Model steered.")
        finally:
            gate.release.set()
        ui.wait_text("steered answer")
        ui.idle()

    text = d.run("prompt", script, monkeypatch)
    assert text.count("read then wait") == 1
    assert text.count("steer correction") == 1
    assert text.count("steered answer") == 1
    assert "abandoned output" not in text
    assert len(d.tape.calls) == 3
    assert d.tape.calls[-1]["messages_json"][-1].find("steer correction") >= 0


def test_daemon_prompt_escape_escape_interrupts_without_late_output(
    interactive_daemon, monkeypatch
):
    d = interactive_daemon
    gate = Gate()
    d.tape.expect(answer("late answer"), gate=gate)

    def script(ui):
        ui.send("interrupt me")
        assert gate.entered.wait(5)
        ui.keys("\x1b\x1b")
        try:
            ui.wait_text("Stopped current turn.")
            ui.idle()
        finally:
            gate.release.set()

    text = d.run("prompt", script, monkeypatch)
    assert text.count("interrupt me") == 1
    assert text.count("Stopped current turn.") == 1
    assert "late answer" not in text


def test_daemon_prompt_tree_navigation_and_model_switch(
    interactive_daemon, monkeypatch
):
    d = interactive_daemon
    d.tape.expect(answer("first answer"))
    d.tape.expect(answer("second answer"))
    selected = []

    def select(roots, **_kwargs):
        assert roots and roots[0].entry.kind == "user"
        assert roots[0].entry.message.display_text_content() == "first prompt"
        target = roots[0].children[0]
        assert target.entry.kind == "assistant"
        selected.append(target.entry.id)
        return TreeSelectorResult("navigate", selected[-1])

    monkeypatch.setattr(
        "yoke.cli.interactive.tree_command.select_tree_entry_interactive", select
    )
    monkeypatch.setattr(
        "yoke.cli.interactive.tree_command._ask_branch_summary_choice",
        lambda: (False, None),
    )
    monkeypatch.setattr(
        "yoke.cli.interactive.model_commands.select_table_item_interactive",
        lambda rows, **_kwargs: next(
            row for row in rows if row.choice.model.id == "oracle-vision"
        ),
    )

    def script(ui):
        ui.send("first prompt")
        ui.wait_text("first answer")
        ui.idle()
        ui.send("second prompt")
        ui.wait_text("second answer")
        ui.idle()
        ui.send("/tree")
        ui.wait_text("Navigated to selected point.")
        ui.send("/model")
        ui.wait_text("model switched to oracle:oracle-vision")

    text = d.run("prompt", script, monkeypatch)
    assert selected
    data = d.connection.session_request("GET")["data"]
    assert data["selection"]["model"] == "oracle-vision"
    assert data["tree"]["leafID"] == selected[-1]
    assert "ctx=100000" in text


def test_daemon_prompt_compact_waits_and_uses_persisted_handoff(
    interactive_daemon, monkeypatch
):
    d = interactive_daemon
    d.tape.expect(answer("before compaction"))
    d.tape.expect(answer("compact handoff"), kind="compaction_summary")
    d.tape.expect(answer("after compaction"))

    def script(ui):
        ui.send("remember constraint")
        ui.wait_text("before compaction")
        ui.idle()
        ui.send("/compact")
        ui.wait_text("context compaction")
        ui.send("continue handoff")
        ui.wait_text("after compaction")
        ui.idle()

    text = d.run("prompt", script, monkeypatch)
    assert text.count("after compaction") == 1
    assert "Nothing to compact" not in text
    assert any("compact handoff" in item for item in d.tape.calls[-1]["messages_json"])


def test_daemon_compaction_failure_releases_prompt_worker(
    interactive_daemon, monkeypatch
):
    from yoke.http.runtime import operations

    compact = operations.force_compact_agent

    def fail_after_summary(*args, **kwargs):
        assert compact(*args, **kwargs) is not None
        raise ValueError("fixture summary failed")

    monkeypatch.setattr(operations, "force_compact_agent", fail_after_summary)
    d = interactive_daemon
    d.tape.expect(answer("initial answer"))
    d.tape.expect(answer("summary result"), kind="compaction_summary")
    d.tape.expect(answer("recovered answer"))

    def script(ui):
        ui.send("initial prompt")
        ui.wait_text("initial answer")
        ui.idle()
        ui.send("/compact")
        ui.wait_text("Compaction failed: fixture summary failed")
        ui.send("continue after failure")
        ui.wait_text("recovered answer")
        ui.idle()

    text = d.run("prompt", script, monkeypatch)
    assert text.count("continue after failure") == 1
    assert text.count("recovered answer") == 1
    assert len(d.tape.calls) == 3
    assert not d.tape.steps
