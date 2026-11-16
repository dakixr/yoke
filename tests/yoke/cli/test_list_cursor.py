"""Shared selection rules across selectors and inspectors."""

from yoke.cli.list_cursor import ListCursor


def test_cursor_clamps_movement_and_follows_selection_through_resize():
    cursor = ListCursor()
    cursor.move(100, 12)
    assert cursor.index == 11
    assert cursor.viewport(12, 4) == slice(8, 12)
    assert cursor.scroll == 8
    assert cursor.viewport(12, 8) == slice(4, 12)
    cursor.move(-100, 12)
    assert cursor.viewport(12, 4) == slice(0, 4)
    cursor.end(0)
    assert cursor.index == 0
    assert cursor.viewport(0, 0) == slice(0, 1)
    assert cursor.scroll == 0


def test_cursor_retains_identity_and_only_follows_new_items_at_the_tail():
    cursor = ListCursor(index=1)
    cursor.refresh(["a", "b", "c"], ["b", "c", "d"], key=str)
    assert cursor.index == 0
    cursor.end(3)
    cursor.refresh(["b", "c", "d"], ["b", "c", "d", "e"], key=str)
    assert cursor.index == 3
    cursor.refresh(["b", "c", "d", "e"], [], key=str)
    assert cursor.index == 0
    cursor.index = 2
    cursor.refresh(["a", "b", "c", "d"], ["a"], key=str)
    assert cursor.index == 0


def test_cursor_discards_an_old_viewport_when_filter_removes_rows():
    cursor = ListCursor(index=35, scroll=30)
    assert cursor.viewport(40, 10) == slice(30, 40)
    assert cursor.viewport(3, 10) == slice(0, 10)
    assert cursor.index == 2
