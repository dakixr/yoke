from __future__ import annotations

# ruff: noqa: F403, F405
# ruff: noqa: ANN002,ANN003,D100,D103,F405,S101

from yoke.cli.interactive import _format_bottom_toolbar

from .support import *  # noqa: F403, F405


def test_prompt_toolkit_renderer_emits_one_tools_divider_per_turn() -> None:
    events: list[str] = []

    renderer = PromptToolkitLiveRenderer(
        begin_tool_block=lambda: events.append("divider"),
        emit_tool=lambda text, failed: events.append(f"tool:{failed}:{text}"),
        emit_agent=lambda text: events.append(f"agent:{text}"),
        emit_commentary=lambda text: events.append(f"commentary:{text}"),
        emit_error=lambda text: events.append(f"error:{text}"),
        emit_notice=lambda text: events.append(f"note:{text}"),
        set_status=lambda _text: None,
    )

    with renderer:
        renderer.handle_event("model_start", {})
        renderer.handle_event(
            "context_compaction",
            {
                "summarized_messages": 4,
                "kept_messages": 3,
                "boundary": "user",
                "reason": "threshold",
                "input_tokens": 12_400,
                "compacted_input_tokens": 2_100,
            },
        )
        renderer.handle_event(
            "tool_execution_start",
            {
                "tool_name": COMMAND_TOOL_NAME,
                "tool_arguments": '{"command":"dir","timeout_seconds":1}',
            },
        )
        renderer.handle_event(
            "tool_execution_start",
            {"tool_name": "read", "tool_arguments": '{"path":"todo.json"}'},
        )
        renderer.handle_event("model_start", {})
        renderer.handle_event(
            "tool_execution_start",
            {"tool_name": "read", "tool_arguments": '{"path":"spec.md"}'},
        )
        renderer.handle_event(
            "assistant_message",
            {"phase": "commentary", "content": "Inspecting results."},
        )
        renderer.handle_event(
            "tool_execution_start",
            {"tool_name": "read", "tool_arguments": '{"path":"after.md"}'},
        )
        renderer.print_agent_output("done")

    with renderer:
        renderer.handle_event("model_start", {})
        renderer.handle_event(
            "tool_execution_start",
            {"tool_name": "read", "tool_arguments": '{"path":"other.md"}'},
        )

    assert events == [
        "note:auto context compaction (from 12k tokens to 2.1k tokens)",
        "divider",
        "tool:False:",
        f'tool:False:{COMMAND_TOOL_NAME} command="dir" timeout_seconds=1',
        'tool:False:read path="todo.json"',
        'tool:False:read path="spec.md"',
        "commentary:Inspecting results.",
        'tool:False:read path="after.md"',
        "agent:done",
        "divider",
        "tool:False:",
        'tool:False:read path="other.md"',
    ]


def test_prompt_toolkit_renderer_opens_tool_block_before_first_commentary() -> None:
    events: list[str] = []

    renderer = PromptToolkitLiveRenderer(
        begin_tool_block=lambda: events.append("divider"),
        emit_tool=lambda text, failed: events.append(f"tool:{failed}:{text}"),
        emit_agent=lambda text: events.append(f"agent:{text}"),
        emit_commentary=lambda text: events.append(f"commentary:{text}"),
        emit_error=lambda text: events.append(f"error:{text}"),
        emit_notice=lambda text: events.append(f"note:{text}"),
        set_status=lambda text: events.append(f"status:{text}"),
    )

    with renderer:
        renderer.handle_event(
            "assistant_message",
            {"phase": "commentary", "content": "Inspecting results."},
        )
        renderer.handle_event(
            "tool_execution_start",
            {"tool_name": "read", "tool_arguments": '{"path":"todo.txt"}'},
        )

    assert events == [
        "status:Thinking",
        "divider",
        "commentary:Inspecting results.",
        "status:Streaming",
        "tool:False:",
        'tool:False:read path="todo.txt"',
        "status:Running tool",
        "status:",
    ]


def test_prompt_toolkit_renderer_updates_context_usage() -> None:
    updates: list[str | None] = []

    renderer = PromptToolkitLiveRenderer(
        begin_tool_block=lambda: None,
        emit_tool=lambda _text, _failed: None,
        emit_agent=lambda _text: None,
        emit_commentary=lambda _text: None,
        emit_error=lambda _text: None,
        emit_notice=lambda _text: None,
        set_status=lambda _text: None,
        set_context_usage=updates.append,
    )

    renderer.handle_event(
        "context_usage",
        {
            "input_tokens": 1536,
            "max_total_tokens": 4000,
            "usage_percent": 38,
        },
    )

    assert updates == ["62% left"]


def test_bottom_toolbar_right_aligns_session_title_when_space_allows() -> None:
    toolbar = _format_bottom_toolbar(
        worker_active=False,
        stop_pending=False,
        status_message="",
        pending_prompts=[],
        context_usage="80% left",
        context_usage_percent=20,
        provider_model="gpt-test",
        root_label="ScriptsCommon",
        session_title="Floating Session Title",
        columns=80,
    )

    text = "".join(t for _s, t in toolbar)

    assert "gpt-test" in text
    assert "80% left" in text
    assert "ScriptsCommon" in text
    assert text.endswith("Floating Session Title ")
    assert len(text) == 80


def test_bottom_toolbar_truncates_session_title_when_space_is_tight() -> None:
    toolbar = _format_bottom_toolbar(
        worker_active=False,
        stop_pending=False,
        status_message="",
        pending_prompts=[],
        provider_model="gpt-test",
        session_title="A Very Long Session Title",
        columns=32,
    )

    text = "".join(t for _s, t in toolbar)

    assert text.startswith(" gpt-test")
    assert text.endswith("A Very Long Sessi... ")
    assert len(text) == 32


def test_bottom_toolbar_shows_short_session_title() -> None:
    toolbar = _format_bottom_toolbar(
        worker_active=False,
        stop_pending=False,
        status_message="",
        pending_prompts=[],
        provider_model="gpt-test",
        session_title="AI",
        columns=32,
    )

    text = "".join(t for _s, t in toolbar)

    assert text == " gpt-test                    AI "


def test_bottom_toolbar_hides_session_title_when_space_is_too_tight() -> None:
    toolbar = _format_bottom_toolbar(
        worker_active=True,
        stop_pending=False,
        status_message="Thinking",
        pending_prompts=[],
        provider_model="gpt-test",
        root_label="ScriptsCommon",
        session_title="Session Title",
        spinner_frame="⠋",
        columns=32,
    )

    text = "".join(t for _s, t in toolbar)

    assert "Thinking" in text
    assert "gpt-test" in text
    assert "ScriptsCommon" in text
    assert "Session Title" not in text


def test_tool_preview_shows_all_arguments_with_truncated_values() -> None:
    preview = format_tool_preview(
        "edit",
        json.dumps(
            {
                "path": "notes.txt",
                "old_string": "alpha " * 30,
                "new_text": "beta",
            }
        ),
    )

    assert preview.startswith('edit path="notes.txt" old_string="alpha alpha')
    assert 'new_text="beta"' in preview
    assert len(preview) <= 225
