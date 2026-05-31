"""Prompt-toolkit session tree selector."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass

from prompt_toolkit.formatted_text.base import StyleAndTextTuples

from yoke.agent.models import ConversationEntry
from yoke.cli.render import truncate_cli_text
from yoke.cli.runtime.selector_ui import selector_page_step
from yoke.cli.runtime.selector_ui import selector_terminal_size
from yoke.cli.runtime.tree import TreeFilterMode
from yoke.cli.runtime.tree import TreeNode
from yoke.cli.runtime.tree import TreeRow
from yoke.cli.runtime.tree import flatten_tree_rows

FILTER_MODES: tuple[TreeFilterMode, ...] = (
    "default",
    "no-tools",
    "user-only",
    "labeled-only",
    "all",
)

BRANCH_STYLES: tuple[str, ...] = (
    "ansired",
    "ansigreen",
    "ansiyellow",
    "ansiblue",
    "ansimagenta",
    "ansicyan",
)


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

    selected_index = 0
    scroll_offset = 0
    search = ""
    filter_mode: TreeFilterMode = "default"
    folded_ids: set[str] = set()
    rows: list[TreeRow] = []

    def rebuild_rows() -> None:
        nonlocal rows, selected_index
        rows = flatten_tree_rows(
            list(roots),
            current_leaf_id=current_leaf_id,
            filter_mode=filter_mode,
            search=search,
            folded_ids=folded_ids,
        )
        if not rows:
            selected_index = 0
            return
        wanted_id = initial_selected_id or current_leaf_id
        if wanted_id and selected_index == 0:
            for index, row in enumerate(rows):
                if row.entry.id == wanted_id:
                    selected_index = index
                    break
        selected_index = max(0, min(selected_index, len(rows) - 1))

    rebuild_rows()

    def formatted_rows() -> StyleAndTextTuples:
        nonlocal scroll_offset
        terminal_columns, terminal_rows = selector_terminal_size()
        body_rows = max(4, terminal_rows - 5)
        scroll_offset = _normalize_scroll(
            selected_index,
            scroll_offset,
            len(rows),
            body_rows,
        )
        visible = rows[scroll_offset : scroll_offset + body_rows]
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
        for index, row in enumerate(visible, start=scroll_offset):
            line = _format_row(row, terminal_columns)
            if index == selected_index:
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
        footer = f"{scroll_offset + len(visible)}/{len(rows)} entries"
        fragments.append(("", footer))
        return fragments

    key_bindings = KeyBindings()
    app: Application[TreeSelectorResult | None]

    @key_bindings.add("down")
    @key_bindings.add("j")
    def _down(event) -> None:
        nonlocal selected_index
        if rows:
            selected_index = min(selected_index + 1, len(rows) - 1)
        event.app.invalidate()

    @key_bindings.add("up")
    @key_bindings.add("k")
    def _up(event) -> None:
        nonlocal selected_index
        selected_index = max(selected_index - 1, 0)
        event.app.invalidate()

    @key_bindings.add("pagedown")
    def _page_down(event) -> None:
        nonlocal selected_index
        if rows:
            selected_index = min(selected_index + selector_page_step(), len(rows) - 1)
        event.app.invalidate()

    @key_bindings.add("pageup")
    def _page_up(event) -> None:
        nonlocal selected_index
        selected_index = max(selected_index - selector_page_step(), 0)
        event.app.invalidate()

    @key_bindings.add("home")
    def _home(event) -> None:
        nonlocal selected_index
        selected_index = 0
        event.app.invalidate()

    @key_bindings.add("end")
    def _end(event) -> None:
        nonlocal selected_index
        if rows:
            selected_index = len(rows) - 1
        event.app.invalidate()

    @key_bindings.add("enter")
    def _accept(event) -> None:
        if rows:
            event.app.exit(
                result=TreeSelectorResult(
                    "select",
                    rows[selected_index].entry.id,
                )
            )

    @key_bindings.add("l")
    def _label(event) -> None:
        if rows:
            event.app.exit(
                result=TreeSelectorResult(
                    "label",
                    rows[selected_index].entry.id,
                )
            )

    @key_bindings.add(" ")
    def _fold(event) -> None:
        if rows:
            entry_id = rows[selected_index].entry.id
            if entry_id in folded_ids:
                folded_ids.remove(entry_id)
            else:
                folded_ids.add(entry_id)
            rebuild_rows()
        event.app.invalidate()

    @key_bindings.add("f")
    def _filter(event) -> None:
        nonlocal filter_mode, selected_index
        index = FILTER_MODES.index(filter_mode)
        filter_mode = FILTER_MODES[(index + 1) % len(FILTER_MODES)]
        selected_index = 0
        rebuild_rows()
        event.app.invalidate()

    @key_bindings.add("backspace")
    @key_bindings.add("c-h")
    def _backspace(event) -> None:
        nonlocal search, selected_index
        search = search[:-1]
        selected_index = 0
        rebuild_rows()
        event.app.invalidate()

    @key_bindings.add("c-c")
    @key_bindings.add("escape")
    def _cancel(event) -> None:
        event.app.exit(result=None)

    @key_bindings.add(Keys.Any)
    def _search(event) -> None:
        nonlocal search, selected_index
        data = event.data
        if data and data.isprintable():
            search += data
            selected_index = 0
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
    suffix = f"{folded}{row.entry.kind}{label}: {text}"
    return _truncate_fragments(
        [*prefix, *marker, ("", suffix)],
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


def _normalize_scroll(
    selected_index: int,
    scroll_offset: int,
    item_count: int,
    body_rows: int,
) -> int:
    if item_count <= body_rows:
        return 0
    if selected_index < scroll_offset:
        return selected_index
    if selected_index >= scroll_offset + body_rows:
        return selected_index - body_rows + 1
    return max(0, min(scroll_offset, item_count - body_rows))
