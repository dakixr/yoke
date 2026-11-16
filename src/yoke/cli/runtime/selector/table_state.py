"""State for interactive table selectors."""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Sequence
from typing import Protocol
from typing import TypeVar

from yoke.cli.list_cursor import ListCursor

ItemT = TypeVar("ItemT")


class _KeyBindingsProtocol(Protocol):
    def add(
        self, *keys: str
    ) -> Callable[[Callable[..., object]], Callable[..., object]]: ...


class _TableSelectorState[ItemT]:
    def __init__(
        self,
        items: Sequence[ItemT],
        *,
        filter_item: Callable[[ItemT, str], bool] | None,
        filter_label: str,
        footer: str,
        subtitle: str | None,
        action_key: str | None,
        action_label: str | None,
        on_action: Callable[[ItemT], None] | None,
    ) -> None:
        self.items = items
        self.filter_item = filter_item
        self.filter_label = filter_label
        self.footer = footer
        self.subtitle = subtitle
        self.action_key = action_key
        self.action_label = action_label
        self.on_action = on_action
        self.cursor = ListCursor()
        self.query = ""
        self.search_mode = False

    def filtered_items(self) -> list[ItemT]:
        if self.filter_item is None or not self.query:
            return list(self.items)
        return [item for item in self.items if self.filter_item(item, self.query)]

    def normalize_selected_index(self, current_items: Sequence[ItemT]) -> None:
        self.cursor.select(self.cursor.index, len(current_items))

    def visible_subtitle(self) -> str | None:
        status_parts: list[str] = []
        if self.filter_item is not None:
            prompt = f"/{self.query}" if self.search_mode else self.query
            if prompt:
                status_parts.append(f"{self.filter_label}: {prompt}")
        if not status_parts:
            return self.subtitle
        status = "  ".join(status_parts)
        return f"{self.subtitle}\n{status}" if self.subtitle else status

    def current_footer(self) -> str:
        if self.search_mode:
            return "Type to search, Enter to keep filter, Esc to clear."
        footer = self.footer
        if self.action_key and self.action_label:
            footer = f"{footer}, {self.action_key} {self.action_label}"
        if self.filter_item is not None:
            footer = f"{footer}, / search"
        return footer

    def move_down(self) -> None:
        self.cursor.move(1, len(self.filtered_items()))

    def move_up(self) -> None:
        self.cursor.move(-1, len(self.filtered_items()))

    def page_down(self) -> None:
        from yoke.cli.runtime.selector.ui import selector_page_step

        self.cursor.move(selector_page_step(), len(self.filtered_items()))

    def page_up(self) -> None:
        from yoke.cli.runtime.selector.ui import selector_page_step

        self.cursor.move(-selector_page_step(), len(self.filtered_items()))

    def move_home(self) -> None:
        self.cursor.select(0, len(self.filtered_items()))

    def move_end(self) -> None:
        self.cursor.end(len(self.filtered_items()))

    def append_search_text(self, text: str) -> None:
        if not text or text.isspace():
            return
        self.query += text
        self.cursor = ListCursor()

    def run_action(self) -> None:
        current_items = self.filtered_items()
        if current_items and self.on_action is not None:
            self.on_action(current_items[self.cursor.index])

    def selected_item(self) -> ItemT | None:
        current_items = self.filtered_items()
        if not current_items:
            return None
        return current_items[self.cursor.index]
