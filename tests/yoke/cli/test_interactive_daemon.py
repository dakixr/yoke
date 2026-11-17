"""Interactive daemon regressions, exercised through both UI implementations."""

from __future__ import annotations


import pytest

from tests.yoke.provider_projection.recording import answer, tool_calls
from yoke.cli.interactive.tree_selector import TreeSelectorResult

from .interactive_daemon_support import (
    interactive_daemon as interactive_daemon,
)


@pytest.mark.parametrize("mode", ["prompt"])
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


@pytest.mark.parametrize("mode", ["basic"])
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
