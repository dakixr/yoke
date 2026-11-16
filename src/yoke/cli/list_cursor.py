"""Selection and viewport rules shared by terminal lists and inspectors."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass


@dataclass(slots=True)
class ListCursor:
    """Own a row selection and its scroll offset independently of rendering."""

    index: int = 0
    scroll: int = 0

    def select(self, index: int, count: int) -> None:
        """Clamp a selection, including an empty or newly filtered list."""
        self.index = max(0, min(index, count - 1))

    def move(self, delta: int, count: int) -> None:
        self.select(self.index + delta, count)

    def end(self, count: int) -> None:
        self.select(count - 1, count)

    def viewport(self, count: int, rows: int) -> slice:
        """Keep the selected row visible after movement, resize, or filtering."""
        rows = max(1, rows)
        self.select(self.index, count)
        self.scroll = max(0, min(self.scroll, count - rows))
        if self.index < self.scroll:
            self.scroll = self.index
        elif self.index >= self.scroll + rows:
            self.scroll = self.index - rows + 1
        return slice(self.scroll, self.scroll + rows)

    def refresh[T, K](
        self, previous: Sequence[T], current: Sequence[T], *, key: Callable[[T], K]
    ) -> None:
        """Preserve identity while refreshing; follow new items only at the tail."""
        at_tail = bool(previous) and self.index >= len(previous) - 1
        selected = (
            key(previous[self.index]) if 0 <= self.index < len(previous) else None
        )
        if at_tail:
            self.end(len(current))
            return
        for index, item in enumerate(current):
            if key(item) == selected:
                self.select(index, len(current))
                return
        self.select(self.index, len(current))
