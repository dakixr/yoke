from __future__ import annotations

# ruff: noqa: F403, F405
# ruff: noqa: ANN003, ANN202, D100, D103, E501, F403, F405, S101

import re

from rich.console import Console

from yoke.cli.interactive.renderer import PromptToolkitLiveRenderer
from yoke.cli.render import format_compaction_note
from yoke.cli.render import InteractiveRenderer
from yoke.cli.render import print_tool_response_divider

from .support import *  # noqa: F403, F405


def test_format_compaction_note_distinguishes_overflow_retry() -> None:
    assert (
        format_compaction_note(
            {
                "reason": "overflow_retry",
                "input_tokens": 12_400,
                "compacted_input_tokens": 2_100,
            }
        )
        == "context overflow retry compaction (from 12k tokens to 2.1k tokens)"
    )


def test_format_compaction_note_keeps_threshold_auto_label() -> None:
    assert (
        format_compaction_note(
            {
                "reason": "threshold",
                "input_tokens": 12_400,
                "compacted_input_tokens": 2_100,
            }
        )
        == "auto context compaction (from 12k tokens to 2.1k tokens)"
    )


def test_commentary_scrollback_keeps_single_tool_divider_per_turn() -> None:
    stdout = CaptureStream()
    console = build_console(stdout)

    print_session_scrollback(
        console,
        [
            Message(
                role="assistant",
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        function=ToolFunction(
                            name="rg", arguments='{"raw_args":"--files"}'
                        ),
                    )
                ],
            ),
            Message(
                role="assistant",
                content="Still checking the read-only path.",
                phase="commentary",
                tool_calls=[
                    ToolCall(
                        id="call-2",
                        function=ToolFunction(
                            name="rg",
                            arguments='{"raw_args":"TODO"}',
                        ),
                    )
                ],
            ),
        ],
    )

    output = stdout.getvalue()
    assert "yoke-tool-calls" not in output
    assert "\nStill checking the read-only path.\n\n" in output
    assert 'rg raw_args="--files"' in output
    assert 'rg raw_args="TODO"' in output


def test_build_console_keeps_terminal_rendering_for_encoded_tty() -> None:
    console = build_console(EncodedTTYCaptureStream())

    assert console.is_terminal is True
    assert console.color_system is not None


def test_scrollback_agent_wraps_long_markdown_list_items() -> None:
    stdout = EncodedTTYCaptureStream()
    console = Console(
        file=stdout,
        force_terminal=True,
        color_system="standard",
        width=60,
        highlight=False,
    )

    print_scrollback_agent(
        console,
        "- lorem impsum on reapeat lorem impsum on reapeat lorem "
        "impsum on reapeat lorem impsum on reapeat lorem impsum on "
        "reapeat lorem impsum on reapeat",
    )

    output = re.sub(r"\x1b\[[0-9;]*m", "", stdout.getvalue())
    assert " • lorem impsum on reapeat lorem impsum on reapeat lorem" in output
    assert "   impsum on reapeat lorem impsum on reapeat lorem impsum on" in output


def test_session_scrollback_matches_live_rendering_after_memory_checkpoint() -> None:
    replay_stdout = CaptureStream()
    replay_console = build_console(replay_stdout)
    live_stdout = CaptureStream()
    live_console = build_console(live_stdout)

    messages = [
        Message.user("old prompt"),
        Message.user(render_memory_message("internal compacted summary")),
        Message(
            role="assistant",
            tool_calls=[
                ToolCall(
                    id="call-1",
                    function=ToolFunction(
                        name="read", arguments='{"path":"todo.json"}'
                    ),
                )
            ],
        ),
        Message.tool(
            "call-1",
            json.dumps({"ok": False, "error": "missing file"}),
        ),
        Message.assistant("final answer"),
    ]

    print_session_scrollback(replay_console, messages)

    live_console.print()
    print_scrollback_tool(live_console, 'read path="todo.json"')
    print_scrollback_tool(live_console, "missing file", failed=True)
    print_tool_response_divider(live_console)
    print_scrollback_agent(live_console, "final answer")

    assert replay_stdout.getvalue() == live_stdout.getvalue()


def test_session_scrollback_preserves_user_image_reference_text() -> None:
    stdout = CaptureStream()
    console = build_console(stdout)

    print_session_scrollback(
        console,
        [
            Message.user(
                [
                    MessageTextContentPart(
                        text="before [yoke-clipboard-h87ietvo.png] after"
                    ),
                    MessageLocalImageContentPart(
                        path="C:/tmp/yoke-clipboard-h87ietvo.png"
                    ),
                ]
            ),
            Message.assistant("ok"),
        ],
    )

    output = stdout.getvalue()
    assert "before [yoke-clipboard-h87ietvo.png] after" in output
    assert "[Image]" not in output


def test_session_scrollback_hides_compacted_context_after_compaction() -> None:
    stdout = CaptureStream()
    console = build_console(stdout)

    print_session_scrollback(
        console,
        [
            *[Message.user(f"old user {index}") for index in range(12)],
            Message.assistant("old assistant answer"),
            Message.user(render_memory_message("internal compacted summary")),
            Message.user("new user after compaction"),
            Message.assistant("new answer"),
        ],
    )

    output = stdout.getvalue()
    for index in range(12):
        assert f"old user {index}" not in output
    assert "old assistant answer" not in output
    assert "internal compacted summary" not in output
    assert "new user after compaction" in output
    assert "new answer" in output


def test_resume_does_not_print_compaction_memory_message(
    tmp_path: Path,
) -> None:
    store = SessionStore()
    store.save(
        "compacted",
        [
            Message.user("older user"),
            Message.user(render_memory_message("internal compacted summary")),
            Message.user("new user"),
            Message.assistant("new answer"),
        ],
        root=tmp_path,
        title="Compacted session",
    )
    prompts = iter(["quit"])

    def fake_input(_: object = "") -> str:
        return next(prompts)

    stdout = CaptureStream()
    stderr = CaptureStream()
    exit_code = run_resume_cli(
        CLIArgs(root=str(tmp_path)),
        "compacted",
        agent=FakeAgent(),
        input_func=fake_input,
        stdout=stdout,
        stderr=stderr,
    )

    output = stdout.getvalue()
    assert exit_code == 0
    assert "older user" not in output
    assert "new user" in output
    assert "new answer" in output
    assert "internal compacted summary" not in output
    assert "Another language model started" not in output


def test_interactive_renderer_tool_response_divider_is_turn_scoped() -> None:
    stdout = CaptureStream()
    renderer = InteractiveRenderer(stdout)

    with renderer:
        renderer.handle_event(
            "tool_execution_start",
            {"tool_name": "read", "tool_arguments": {"path": "todo.txt"}},
        )

    renderer.print_agent_output("first response")
    renderer.print_agent_output("second response")

    output = stdout.getvalue()
    assert 'read path="todo.txt"' in output
    assert output.count("---\n") == 1


def test_prompt_toolkit_renderer_tool_response_divider_is_turn_scoped() -> None:
    dividers: list[str] = []
    agent_outputs: list[str] = []
    tool_outputs: list[tuple[str, bool]] = []
    renderer = PromptToolkitLiveRenderer(
        begin_tool_block=lambda: None,
        emit_tool=lambda text, failed: tool_outputs.append((text, failed)),
        emit_agent=agent_outputs.append,
        emit_commentary=lambda text: None,
        emit_error=lambda text: None,
        emit_notice=lambda text: None,
        set_status=lambda text: None,
        emit_tool_response_divider=lambda: dividers.append("divider"),
    )

    with renderer:
        renderer.handle_event(
            "tool_execution_start",
            {"tool_name": "read", "tool_arguments": {"path": "todo.txt"}},
        )
        renderer.print_agent_output("first response")

    with renderer:
        pass

    renderer.print_agent_output("second response")

    assert tool_outputs == [("", False), ('read path="todo.txt"', False)]
    assert agent_outputs == ["first response", "second response"]
    assert dividers == ["divider"]


def test_prompt_toolkit_renderer_keeps_tool_response_divider_after_context_exit() -> (
    None
):
    dividers: list[str] = []
    agent_outputs: list[str] = []
    renderer = PromptToolkitLiveRenderer(
        begin_tool_block=lambda: None,
        emit_tool=lambda _text, _failed: None,
        emit_agent=agent_outputs.append,
        emit_commentary=lambda _text: None,
        emit_error=lambda _text: None,
        emit_notice=lambda _text: None,
        set_status=lambda _text: None,
        emit_tool_response_divider=lambda: dividers.append("divider"),
    )

    with renderer:
        renderer.handle_event(
            "tool_execution_start",
            {"tool_name": "read", "tool_arguments": {"path": "todo.txt"}},
        )

    renderer.print_agent_output("response")

    assert agent_outputs == ["response"]
    assert dividers == ["divider"]
