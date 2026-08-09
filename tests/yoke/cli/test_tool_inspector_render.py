"""Regression tests for tool inspector rendering."""

# ruff: noqa: S101

from prompt_toolkit.formatted_text import HTML

from yoke.cli.interactive.tool_inspector import render
from yoke.cli.interactive.tool_inspector.app import _refresh_entries
from yoke.cli.interactive.tool_inspector.app import ToolInspectorState
from yoke.cli.interactive.tool_inspector.render import move_selection
from yoke.cli.interactive.tool_inspector.render import render_view_html
from yoke.cli.interactive.tool_inspector.render import sidebar_items
from yoke.cli.interactive.tool_inspector.trace import ToolTraceEntry
from yoke.cli.interactive.tool_inspector.trace import ToolTraceStore


def test_sparse_sidebar_keeps_divider_at_fixed_width(monkeypatch) -> None:
    """Empty sidebar rows must preserve the two-pane layout width."""
    monkeypatch.setattr(
        "yoke.cli.interactive.tool_inspector.render.terminal_size",
        lambda: (120, 20),
    )
    entries = [
        ToolTraceEntry(
            tool_call_id="one",
            tool_name="read",
            result={"ok": True},
        )
    ]
    state = ToolInspectorState(entries)

    formatted = HTML(
        render_view_html(state, sidebar_items(entries))
    ).__pt_formatted_text__()
    lines = "".join(fragment[1] for fragment in formatted).splitlines()
    body_lines = lines[3:-2]

    assert len(body_lines) == 15
    assert {line.index("│") for line in body_lines} == {41}


def test_navigation_to_ansi_tool_output_does_not_break_html() -> None:
    """Selecting terminal control output must remain valid inspector HTML."""
    entries = [
        ToolTraceEntry(
            tool_call_id="safe",
            tool_name="read",
            result={"ok": True, "content": "safe"},
        ),
        ToolTraceEntry(
            tool_call_id="ansi",
            tool_name="exec_command",
            result={
                "ok": True,
                "output": ("\x00\x1b[31mboom\x1b[0m\ud800\n<ansired>unterminated"),
            },
        ),
    ]
    state = ToolInspectorState(entries)
    visible = sidebar_items(entries)
    state.selected_index = 0

    HTML(render_view_html(state, visible)).__pt_formatted_text__()
    move_selection(state, visible, 1)
    formatted = HTML(render_view_html(state, visible)).__pt_formatted_text__()

    assert state.selected_index == 1
    assert any("␀␛[31mboom␛[0m�" in item[1] for item in formatted)
    assert any("<ansired>unterminated" in item[1] for item in formatted)


def test_refresh_skips_snapshot_when_trace_revision_is_unchanged() -> None:
    """Navigation redraws must not rebuild an unchanged live transcript."""
    entries = [ToolTraceEntry(tool_call_id="one", tool_name="read")]
    state = ToolInspectorState(entries, source_revision=4)
    revision = 4
    provider_calls = 0

    def entries_provider() -> list[ToolTraceEntry]:
        nonlocal provider_calls
        provider_calls += 1
        return [*entries, ToolTraceEntry(tool_call_id="two", tool_name="rg")]

    _refresh_entries(state, entries_provider, lambda: revision)
    assert provider_calls == 0

    revision = 5
    _refresh_entries(state, entries_provider, lambda: revision)
    assert provider_calls == 1
    assert [entry.tool_call_id for entry in state.entries] == ["one", "two"]
    assert state.data_revision == 1


def test_render_caches_selected_detail_until_data_or_mode_changes(
    monkeypatch,
) -> None:
    """Repeated prompt-toolkit redraws must reuse expensive detail layout."""
    monkeypatch.setattr(render, "terminal_size", lambda: (120, 20))
    entries = [
        ToolTraceEntry(
            tool_call_id="one",
            tool_name="read",
            result={"ok": True, "content": "x" * 20_000},
        )
    ]
    state = ToolInspectorState(entries)
    original_detail_text = render.detail_text
    detail_calls = 0

    def counted_detail_text(entry, render_state) -> str:
        nonlocal detail_calls
        detail_calls += 1
        return original_detail_text(entry, render_state)

    monkeypatch.setattr(render, "detail_text", counted_detail_text)
    visible = sidebar_items(entries)

    render_view_html(state, visible)
    render_view_html(state, visible)
    assert detail_calls == 1

    state.raw = True
    render_view_html(state, visible)
    assert detail_calls == 2


def test_live_output_is_bounded_compacted_and_snapshot_is_stable() -> None:
    """High-volume deltas must keep bounded work and immutable snapshots."""
    store = ToolTraceStore()
    store.record_start({"tool_call_id": "run", "tool_name": "exec_command"})
    for _ in range(1_000):
        store.record_output_delta(
            {
                "tool_call_id": "run",
                "tool_name": "exec_command",
                "stream": "stdout",
                "text": "x" * 100,
            }
        )

    snapshot = store.snapshot()
    chunks = snapshot[0].output_chunks or []
    snapshot_text = "".join(chunk.text for chunk in chunks)
    assert 45_000 <= len(snapshot_text) <= 50_000
    assert len(chunks) <= 14

    store.record_output_delta(
        {
            "tool_call_id": "run",
            "tool_name": "exec_command",
            "stream": "stdout",
            "text": "new",
        }
    )
    assert "".join(chunk.text for chunk in chunks) == snapshot_text


def test_broken_trace_subscriber_does_not_block_other_subscribers() -> None:
    """Inspector callback failures must not interrupt tool execution."""
    store = ToolTraceStore()
    notifications: list[str] = []

    def broken_subscriber() -> None:
        raise RuntimeError("closed UI")

    store.subscribe(broken_subscriber)
    store.subscribe(lambda: notifications.append("notified"))

    store.record_start({"tool_call_id": "one", "tool_name": "read"})
    assert notifications == ["notified"]
