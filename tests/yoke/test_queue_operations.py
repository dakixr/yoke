from __future__ import annotations

# ruff: noqa: D100,D103,S101

import pytest

from yoke.session.queue import (
    PersistedPendingInput,
    move_queue_item,
    next_eligible_index,
)


@pytest.mark.parametrize(
    ("index", "target", "after", "expected"),
    [
        (0, 2, False, ["b", "a", "c"]),
        (0, 2, True, ["b", "c", "a"]),
        (2, 0, False, ["c", "a", "b"]),
        (2, 0, True, ["a", "c", "b"]),
        (1, 1, False, ["a", "b", "c"]),
        (1, 1, True, ["a", "c", "b"]),
    ],
)
def test_original_position_move_semantics(index, target, after, expected) -> None:
    items = ["a", "b", "c"]
    move_queue_item(items, index, target, after=after)
    assert items == expected


def test_delivery_priority_preserves_layer_eligibility() -> None:
    items = [
        PersistedPendingInput(id="queue", prompt="", created_at="", kind="queued"),
        PersistedPendingInput(
            id="paused", prompt="", created_at="", kind="steering", paused=True
        ),
        PersistedPendingInput(
            id="native", prompt="", created_at="", kind="steering", continuation=True
        ),
        PersistedPendingInput(id="cli", prompt="", created_at="", kind="steering"),
    ]
    assert next_eligible_index(items, allow_queue=True) == 2
    assert (
        next_eligible_index(
            items, allow_queue=True, runnable=lambda item: not item.continuation
        )
        == 3
    )
    items[3].paused = True
    assert (
        next_eligible_index(
            items, allow_queue=False, runnable=lambda item: not item.continuation
        )
        is None
    )
    assert (
        next_eligible_index(
            items, allow_queue=True, runnable=lambda item: not item.continuation
        )
        == 0
    )
