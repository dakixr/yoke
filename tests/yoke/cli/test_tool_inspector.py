from __future__ import annotations

# ruff: noqa: ANN002,ANN003,D100,D103,S101

import json
from typing import cast

from yoke.agent.models import Message
from yoke.agent.models import ToolCall
from yoke.agent.models import ToolFunction
from yoke.cli.interactive.tool_inspector import ToolInspectorState
from yoke.cli.interactive.tool_inspector_render import detail_text
from yoke.cli.interactive.tool_inspector_render import render_view
from yoke.cli.interactive.tool_inspector_render import render_view_html
from yoke.cli.interactive.tool_inspector_render import sidebar_items
from yoke.cli.interactive.tool_trace import ToolTraceStore
from yoke.cli.interactive.tool_trace import ToolTraceEntry
from yoke.cli.interactive.tool_trace import entries_from_messages
from yoke.cli.interactive.tool_trace import merge_trace_entries


def test_entries_from_messages_pairs_tool_calls_with_full_results() -> None:
    messages = [
        Message(
            role="assistant",
            tool_calls=[
                ToolCall(
                    id="call-1",
                    function=ToolFunction(
                        name="read",
                        arguments=json.dumps({"path": "notes.txt"}),
                    ),
                )
            ],
        ),
        Message.tool(
            "call-1",
            json.dumps({"ok": True, "content": "hello"}),
        ),
    ]

    entries = entries_from_messages(messages)

    assert len(entries) == 1
    assert entries[0].tool_name == "read"
    assert entries[0].raw_arguments == '{"path": "notes.txt"}'
    assert entries[0].result == {"ok": True, "content": "hello"}
    assert entries[0].status == "ok"


def test_entries_from_messages_attaches_user_and_assistant_context() -> None:
    messages = [
        Message.user("please inspect the config"),
        Message(
            role="assistant",
            content="I will read the config first.",
            tool_calls=[
                ToolCall(
                    id="call-1",
                    function=ToolFunction(
                        name="read",
                        arguments=json.dumps({"path": "config.json"}),
                    ),
                )
            ],
        ),
    ]

    entries = entries_from_messages(messages)

    assert entries[0].context is not None
    assert [(item.role, item.text) for item in entries[0].context] == [
        ("user", "please inspect the config"),
        ("assistant", "I will read the config first."),
    ]


def test_live_trace_store_records_executed_arguments_and_failures() -> None:
    store = ToolTraceStore()

    store.record_event(
        "tool_execution_start",
        {
            "iteration": 2,
            "tool_name": "powershell",
            "tool_call_id": "call-2",
            "tool_arguments": '{"command":"pytest"}',
        },
    )
    store.record_event(
        "tool_execution_end",
        {
            "iteration": 2,
            "tool_name": "powershell",
            "tool_call_id": "call-2",
            "ok": False,
            "executed_arguments": {"command": "pytest", "timeout": 60},
            "result": {"ok": False, "stderr": "failed"},
        },
    )

    entry = store.snapshot()[0]

    assert entry.tool_name == "powershell"
    assert entry.iteration == 2
    assert entry.executed_arguments == {"command": "pytest", "timeout": 60}
    assert entry.result == {"ok": False, "stderr": "failed"}
    assert entry.status == "failed"
    assert entry.duration_seconds is not None


def test_merge_trace_entries_prefers_live_details() -> None:
    completed = entries_from_messages(
        [
            Message(
                role="assistant",
                tool_calls=[
                    ToolCall(
                        id="call-3",
                        function=ToolFunction(
                            name="read",
                            arguments=json.dumps({"path": "old.txt"}),
                        ),
                    )
                ],
            )
        ]
    )
    store = ToolTraceStore()
    store.record_event(
        "tool_execution_end",
        {
            "tool_name": "read",
            "tool_call_id": "call-3",
            "ok": True,
            "executed_arguments": {"path": "new.txt"},
            "result": {"ok": True, "content": "done"},
        },
    )

    merged = merge_trace_entries(completed, store.snapshot())

    assert len(merged) == 1
    assert merged[0].raw_arguments == '{"path": "old.txt"}'
    assert merged[0].executed_arguments == {"path": "new.txt"}
    assert merged[0].result == {"ok": True, "content": "done"}


def test_tool_inspector_detail_text_formats_text_result_blocks() -> None:
    entry = ToolTraceStore()
    entry.record_event(
        "tool_execution_end",
        {
            "tool_name": "python_exec",
            "tool_call_id": "call-4",
            "ok": True,
            "result": {
                "ok": True,
                "exit_status": 0,
                "stdout": "line 1\nline 2",
            },
        },
    )

    text = detail_text(entry.snapshot()[0], ToolInspectorState(entries=[]))

    assert "Output" in text
    assert "[STDOUT]\n1 │ line 1\n2 │ line 2" in text
    assert '"exit_status": 0' in text


def test_tool_inspector_starts_on_last_sidebar_item() -> None:
    state = ToolInspectorState(
        entries=[
            ToolTraceEntry(tool_call_id="call-1", tool_name="read"),
            ToolTraceEntry(tool_call_id="call-2", tool_name="edit"),
        ]
    )

    assert state.selected_index == 1


def test_tool_inspector_starts_after_context_rows() -> None:
    entries = entries_from_messages(
        [
            Message.user("inspect this"),
            Message(
                role="assistant",
                content="I will read it.",
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        function=ToolFunction(
                            name="read",
                            arguments=json.dumps({"path": "src/app.py"}),
                        ),
                    )
                ],
            ),
        ]
    )
    state = ToolInspectorState(entries=entries)

    assert state.selected_index == 2


def test_tool_inspector_footer_shows_detail_scroll_position(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "yoke.cli.interactive.tool_inspector_render.terminal_size",
        lambda: (80, 10),
    )
    state = ToolInspectorState(
        entries=[
            ToolTraceEntry(
                tool_call_id="call-1",
                tool_name="python_exec",
                result={
                    "ok": True,
                    "stdout": "\n".join(str(i) for i in range(20)),
                },
                status="ok",
            )
        ]
    )
    state.detail_scroll = 4

    lines = render_view(state, state.entries)

    assert "TOOLS focused" in lines[-1]
    state.active_pane = "detail"
    lines = render_view(state, state.entries)
    assert "DETAIL focused" in lines[-1]
    assert "detail 5-9/" in lines[-1]


def test_tool_inspector_clamps_negative_detail_scroll(monkeypatch) -> None:
    monkeypatch.setattr(
        "yoke.cli.interactive.tool_inspector_render.terminal_size",
        lambda: (80, 10),
    )
    state = ToolInspectorState(
        entries=[ToolTraceEntry(tool_call_id="call-1", tool_name="read")]
    )
    state.detail_scroll = -100

    render_view(state, state.entries)

    assert state.detail_scroll == 0


def test_tool_inspector_sidebar_colors_statuses(monkeypatch) -> None:
    monkeypatch.setattr(
        "yoke.cli.interactive.tool_inspector_render.terminal_size",
        lambda: (80, 10),
    )
    state = ToolInspectorState(
        entries=[
            ToolTraceEntry(
                tool_call_id="call-1",
                tool_name="read",
                status="ok",
            ),
            ToolTraceEntry(
                tool_call_id="call-2",
                tool_name="apply_patch",
                status="failed",
            ),
        ]
    )

    html = render_view_html(state, sidebar_items(state.entries))

    assert "<ansigreen>" in html
    assert "<ansired>" in html


def test_tool_inspector_sidebar_shows_conversation_context(monkeypatch) -> None:
    monkeypatch.setattr(
        "yoke.cli.interactive.tool_inspector_render.terminal_size",
        lambda: (100, 16),
    )
    entries = entries_from_messages(
        [
            Message.user("why was this file read?"),
            Message(
                role="assistant",
                content="I need the current implementation.",
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        function=ToolFunction(
                            name="read",
                            arguments=json.dumps({"path": "src/app.py"}),
                        ),
                    )
                ],
            ),
        ]
    )
    state = ToolInspectorState(entries=entries)

    html = render_view_html(state, sidebar_items(state.entries))

    assert "usr why was this file read?" in html
    assert "<ansiblue>  asst I need the current implem…</ansiblue>" in html
    assert "&gt; ? read" in html


def test_tool_inspector_context_rows_are_selectable(monkeypatch) -> None:
    monkeypatch.setattr(
        "yoke.cli.interactive.tool_inspector_render.terminal_size",
        lambda: (100, 16),
    )
    entries = entries_from_messages(
        [
            Message.user("why this tool?"),
            Message(
                role="assistant",
                content="Because I need evidence.",
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        function=ToolFunction(
                            name="read",
                            arguments=json.dumps({"path": "src/app.py"}),
                        ),
                    )
                ],
            ),
        ]
    )
    state = ToolInspectorState(entries=entries)
    state.selected_index = 1

    html = render_view_html(state, sidebar_items(state.entries))

    assert "&gt; asst Because I need evidence." in html
    assert "Assistant Message" in html
    assert "Because I need evidence." in html


def test_tool_inspector_highlights_active_pane_and_dims_inactive_sidebar(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "yoke.cli.interactive.tool_inspector_render.terminal_size",
        lambda: (80, 10),
    )
    state = ToolInspectorState(
        entries=[
            ToolTraceEntry(
                tool_call_id="call-1",
                tool_name="read",
                status="ok",
            )
        ]
    )

    sidebar_html = render_view_html(state, state.entries)
    state.active_pane = "detail"
    detail_html = render_view_html(state, state.entries)

    assert "<reverse><ansicyan> TOOLS " in sidebar_html
    assert "<reverse><ansicyan> DETAIL " in detail_html
    assert "<ansibrightblack>" in detail_html


def test_tool_inspector_arguments_decode_escaped_newlines() -> None:
    entry = ToolTraceEntry(
        tool_call_id="call-1",
        tool_name="apply_patch",
        raw_arguments=json.dumps(
            {"input": "line 1\\nline 2", "timeout": 180}
        ),
    )

    text = detail_text(entry, ToolInspectorState(entries=[]))

    assert "input   │\n1 │ line 1\n2 │ line 2" in text
    assert "timeout │ 180" in text
    assert '"input"' not in text


def test_tool_inspector_result_shows_errors_before_stdout() -> None:
    entry = ToolTraceEntry(
        tool_call_id="call-1",
        tool_name="bash",
        result={
            "ok": False,
            "stdout": "progress",
            "stderr": "boom",
            "exit_status": 1,
        },
        status="failed",
    )

    text = detail_text(entry, ToolInspectorState(entries=[]))

    assert text.index("[STDERR]") < text.index("[STDOUT]")
    assert "[META]" in text


def test_tool_inspector_html_colors_detail_sections(monkeypatch) -> None:
    monkeypatch.setattr(
        "yoke.cli.interactive.tool_inspector_render.terminal_size",
        lambda: (100, 24),
    )
    state = ToolInspectorState(
        entries=[
            ToolTraceEntry(
                tool_call_id="call-1",
                tool_name="bash",
                raw_arguments=json.dumps({"command": "pytest"}),
                result={"ok": False, "stderr": "boom", "exit_status": 1},
                status="failed",
            )
        ]
    )

    html = render_view_html(state, state.entries)

    assert "<ansicyan>╭─ Arguments" in html
    assert "<ansired><b>[STDERR]</b></ansired>" in html
    assert '<style fg="#777777"><b>[META]</b></style>' in html
    assert "<ansicyan>command</ansicyan>" in html


def test_tool_inspector_html_keeps_blank_line_numbers_gray(monkeypatch) -> None:
    monkeypatch.setattr(
        "yoke.cli.interactive.tool_inspector_render.terminal_size",
        lambda: (100, 16),
    )
    state = ToolInspectorState(
        entries=[
            ToolTraceEntry(
                tool_call_id="call-1",
                tool_name="apply_patch",
                raw_arguments=json.dumps({"input": "before\n\nafter"}),
            )
        ]
    )

    html = render_view_html(state, state.entries)

    assert '<style fg="#777777">2  │</style>' in html
    assert "<ansicyan>2</ansicyan>" not in html


def test_tool_inspector_html_escapes_output_lines_starting_with_angle_bracket(
    monkeypatch,
) -> None:
    from prompt_toolkit.formatted_text import HTML

    monkeypatch.setattr(
        "yoke.cli.interactive.tool_inspector_render.terminal_size",
        lambda: (100, 16),
    )
    state = ToolInspectorState(
        entries=[
            ToolTraceEntry(
                tool_call_id="call-1",
                tool_name="bash",
                result={"ok": True, "stdout": "<not-a-tag>\n</broken>"},
                status="ok",
            )
        ]
    )

    html = render_view_html(state, state.entries)

    HTML(html)
    assert "&lt;not-a-tag&gt;" in html
    assert "&lt;/broken&gt;" in html


def test_tool_inspector_scroll_wheel_uses_active_pane() -> None:
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.key_binding.key_processor import KeyPressEvent
    from prompt_toolkit.keys import Keys
    from yoke.cli.interactive.tool_inspector import _register_tool_inspector_keys

    state = ToolInspectorState(
        entries=[
            ToolTraceEntry(tool_call_id="call-1", tool_name="read"),
            ToolTraceEntry(tool_call_id="call-2", tool_name="edit"),
        ]
    )
    state.active_pane = "detail"
    key_bindings = KeyBindings()
    _register_tool_inspector_keys(
        key_bindings,
        state=state,
        visible_entries=lambda: state.entries,
        any_key="a",
    )

    binding = next(
        item for item in key_bindings.bindings if item.keys == (Keys.ScrollDown,)
    )

    class FakeApp:
        def invalidate(self) -> None:
            return None

    class FakeEvent:
        app = FakeApp()

    binding.handler(cast(KeyPressEvent, FakeEvent()))

    assert state.selected_index == 1
    assert state.detail_scroll == 1
