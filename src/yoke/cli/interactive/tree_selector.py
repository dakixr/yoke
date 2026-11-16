"""Prompt-toolkit session tree selector."""

from __future__ import annotations

from yoke.cli.list_cursor import ListCursor

from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass

from prompt_toolkit.formatted_text.base import StyleAndTextTuples

from yoke.agent.models import ConversationEntry
from yoke.cli.render import truncate_cli_text
from yoke.cli.runtime.selector.ui import selector_page_step
from yoke.cli.runtime.selector.ui import selector_terminal_size
from yoke.cli.runtime.terminal_output_gate import (
    suppress_terminal_output_for_fullscreen,
)
from yoke.cli.runtime.tree_view import TreeFilterMode
from yoke.cli.runtime.tree_view import TreeNode
from yoke.cli.runtime.tree_view import TreeRow
from yoke.cli.runtime.tree_view import default_folded_tree_ids
from yoke.cli.runtime.tree_view import flatten_tree_rows

FILTER_MODES: tuple[TreeFilterMode, ...] = (
    "default",
    "no-tools",
    "user-only",
    "labeled-only",
    "all",
)

KIND_STYLES: dict[str, str] = {
    "instruction": "ansibrightblack bold",
    "user": "ansigreen bold",
    "assistant": "ansiblue bold",
    "assistant_tool_calls": "ansimagenta bold",
    "tool_result": "ansicyan bold",
    "tool_context": "ansibrightblack bold",
    "control": "ansiyellow bold",
    "memory_snapshot": "ansibrightmagenta bold",
    "skill_event": "ansibrightcyan bold",
    "compaction_summary": "ansibrightyellow bold",
    "branch_summary": "ansibrightblue bold",
}


@dataclass(slots=True)
class TreeSelectorResult:
    """Result returned by the tree selector."""

    action: str
    entry_id: str | None = None


def select_tree_entry_interactive(  # noqa: C901
    roots: Sequence[TreeNode],
    *,
    current_leaf_id: str | None,
    initial_selected_id: str | None = None,
) -> TreeSelectorResult | None:
    """Open a keyboard-driven tree selector."""
    from prompt_toolkit.application import Application
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.layout import Layout
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.layout.controls import FormattedTextControl

    cursor = ListCursor()
    search = ""
    filter_mode: TreeFilterMode = "default"
    folded_ids = default_folded_tree_ids(roots)
    rows: list[TreeRow] = []

    def rebuild_rows() -> None:
        nonlocal rows
        rows = flatten_tree_rows(
            list(roots),
            current_leaf_id=current_leaf_id,
            filter_mode=filter_mode,
            search=search,
            folded_ids=set() if search else folded_ids,
        )
        if not rows:
            cursor.index = 0
            return
        wanted_id = initial_selected_id or current_leaf_id
        if wanted_id and cursor.index == 0:
            for index, row in enumerate(rows):
                if row.entry.id == wanted_id:
                    cursor.index = index
                    break
        cursor.select(cursor.index, len(rows))

    rebuild_rows()

    def formatted_rows() -> StyleAndTextTuples:
        terminal_columns, terminal_rows = selector_terminal_size()
        body_rows = max(4, terminal_rows - 5)
        visible = rows[cursor.viewport(len(rows), body_rows)]
        title = "Session Tree"
        subtitle = (
            f"filter={filter_mode} search={search or '∅'}  "
            "enter select · f filter · l label · space fold · esc cancel"
        )
        fragments: StyleAndTextTuples = []

        def append_line(
            text: str | None = None,
            style: str = "",
            *,
            line_fragments: StyleAndTextTuples | None = None,
        ) -> None:
            if line_fragments is not None:
                fragments.extend(line_fragments)
            elif text is not None:
                fragments.append((style, text))
            fragments.append(("", "\n"))

        append_line(title)
        append_line(subtitle)
        append_line("")
        for index, row in enumerate(visible, start=cursor.scroll):
            line = _format_row(row, terminal_columns)
            if index == cursor.index:
                append_line(
                    line_fragments=_reverse_fragments(
                        line,
                        terminal_columns,
                    )
                )
            else:
                append_line(line_fragments=line)
        if not visible:
            append_line("No matching entries")
        append_line("")
        footer = f"{cursor.scroll + len(visible)}/{len(rows)} entries"
        fragments.append(("", footer))
        return fragments

    key_bindings = KeyBindings()
    app: Application[TreeSelectorResult | None]

    @key_bindings.add("down")
    @key_bindings.add("j")
    def _down(event) -> None:
        if rows:
            cursor.move(1, len(rows))
        event.app.invalidate()

    @key_bindings.add("up")
    @key_bindings.add("k")
    def _up(event) -> None:
        cursor.move(-1, len(rows))
        event.app.invalidate()

    @key_bindings.add("pagedown")
    def _page_down(event) -> None:
        if rows:
            cursor.move(selector_page_step(), len(rows))
        event.app.invalidate()

    @key_bindings.add("pageup")
    def _page_up(event) -> None:
        cursor.move(-selector_page_step(), len(rows))
        event.app.invalidate()

    @key_bindings.add("home")
    def _home(event) -> None:
        cursor.index = 0
        event.app.invalidate()

    @key_bindings.add("end")
    def _end(event) -> None:
        if rows:
            cursor.end(len(rows))
        event.app.invalidate()

    @key_bindings.add("enter")
    def _accept(event) -> None:
        if rows:
            event.app.exit(
                result=TreeSelectorResult(
                    "select",
                    rows[cursor.index].entry.id,
                )
            )

    @key_bindings.add("l")
    def _label(event) -> None:
        if rows:
            event.app.exit(
                result=TreeSelectorResult(
                    "label",
                    rows[cursor.index].entry.id,
                )
            )

    @key_bindings.add(" ")
    def _fold(event) -> None:
        if rows:
            entry_id = rows[cursor.index].entry.id
            if entry_id in folded_ids:
                folded_ids.remove(entry_id)
            else:
                folded_ids.add(entry_id)
            rebuild_rows()
        event.app.invalidate()

    @key_bindings.add("f")
    def _filter(event) -> None:
        nonlocal filter_mode
        index = FILTER_MODES.index(filter_mode)
        filter_mode = FILTER_MODES[(index + 1) % len(FILTER_MODES)]
        cursor.index = 0
        rebuild_rows()
        event.app.invalidate()

    @key_bindings.add("backspace")
    @key_bindings.add("c-h")
    def _backspace(event) -> None:
        nonlocal search
        search = search[:-1]
        cursor.index = 0
        rebuild_rows()
        event.app.invalidate()

    @key_bindings.add("c-c")
    @key_bindings.add("escape")
    def _cancel(event) -> None:
        event.app.exit(result=None)

    @key_bindings.add(Keys.Any)
    def _search(event) -> None:
        nonlocal search
        data = event.data
        if data and data.isprintable():
            search += data
            cursor.index = 0
            rebuild_rows()
            event.app.invalidate()

    control = FormattedTextControl(formatted_rows, focusable=True)
    app = Application(
        layout=Layout(
            Window(
                content=control,
                always_hide_cursor=True,
                wrap_lines=False,
            )
        ),
        key_bindings=key_bindings,
        full_screen=True,
        mouse_support=False,
    )
    with suppress(EOFError, KeyboardInterrupt):
        with suppress_terminal_output_for_fullscreen():
            return app.run()
    return None


def prompt_tree_label(default: str | None = None) -> str | None:
    """Prompt for a tree label."""
    from prompt_toolkit import prompt

    with suppress(EOFError, KeyboardInterrupt):
        return prompt("Label (empty clears): ", default=default or "")
    return None


def _format_row(
    row: TreeRow,
    terminal_columns: int,
) -> StyleAndTextTuples:
    folded = "+ " if row.folded else ""
    label = f" [{row.label}]" if row.label else ""
    text = _entry_text(row.entry)
    prefix = _format_branch_prefix(row.graph_prefix or "")
    marker = [("ansiyellow", "● ")] if row.current else []
    kind_style = KIND_STYLES.get(row.entry.kind, "bold")
    return _truncate_fragments(
        [
            *prefix,
            *marker,
            ("", folded),
            (kind_style, row.entry.kind),
            ("ansibrightblack", label),
            ("", f": {text}"),
        ],
        max(20, terminal_columns - 1),
    )


def _format_branch_prefix(prefix: str) -> StyleAndTextTuples:
    if not prefix:
        return []
    return [("", prefix)]


def _truncate_fragments(
    fragments: StyleAndTextTuples,
    width: int,
) -> StyleAndTextTuples:
    plain_text = "".join(text for _, text, *_ in fragments)
    truncated = truncate_cli_text(plain_text, width)
    remaining = len(truncated)
    if remaining <= 0:
        return []
    output: StyleAndTextTuples = []
    for fragment in fragments:
        style = fragment[0]
        text = fragment[1]
        if remaining <= 0:
            break
        piece = text[:remaining]
        remaining -= len(piece)
        output.append((style, piece))
    return output


def _reverse_fragments(
    fragments: StyleAndTextTuples,
    terminal_columns: int,
) -> StyleAndTextTuples:
    reversed_fragments: StyleAndTextTuples = []
    line_width = 0
    for fragment in fragments:
        style = fragment[0]
        combined_style = f"{style} reverse".strip()
        reversed_fragments.append((combined_style, fragment[1]))
        line_width += len(fragment[1])
    padding = max(0, terminal_columns - line_width)
    if padding:
        reversed_fragments.append(("reverse", " " * max(0, padding - 1)))
    return reversed_fragments


def _entry_text(entry: ConversationEntry) -> str:
    if entry.message is not None:
        return _single_line_preview(entry.message.display_text_content() or "")
    summary = entry.metadata.get("summary")
    return _single_line_preview(summary) if isinstance(summary, str) else ""


def _single_line_preview(text: str) -> str:
    return " ".join(text.split())
