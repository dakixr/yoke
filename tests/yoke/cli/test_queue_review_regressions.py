"""Queue revision checks must also cover stale mirrors with no runnable item."""

from __future__ import annotations

from pathlib import Path
from threading import Lock

import pytest

from yoke.cli.interactive.common import PendingPrompt, PromptCliState
from yoke.cli.interactive.prompt.turns import finish_prompt_turn
from yoke.cli.interactive.queue.persistence import (
    load_prompt_queue_state,
    persist_prompt_queue,
)
from yoke.session.queue import (
    PersistedPendingInput,
    load_prompt_queue_snapshot,
    prompt_queue_transaction,
)

from .support import active_session_for


@pytest.mark.parametrize("initially_paused", [False, True])
def test_finish_turn_refreshes_an_empty_or_paused_mirror(
    tmp_path: Path, initially_paused: bool
) -> None:
    active = active_session_for(tmp_path)
    original = PendingPrompt("old prompt", id="item", paused=True)
    persist_prompt_queue(active, [original] if initially_paused else [])
    loaded = load_prompt_queue_state(active)
    state = PromptCliState(
        messages=[],
        pending_prompts=loaded.prompts,
        pending_images=loaded.pending_images,
        queue_revision=loaded.revision,
        queue_session_id=active.id,
    )
    with prompt_queue_transaction(active.store.directory, active.id) as transaction:
        transaction.snapshot.prompts = [
            PersistedPendingInput(
                id="item",
                prompt="authoritative prompt",
                paused=False,
                created_at=original.created_at,
            )
        ]
        transaction.snapshot.revision += 1
        transaction.commit()

    selected, shutting_down = finish_prompt_turn(
        state=state,
        state_lock=Lock(),
        active_session=active,
        request_context_usage=lambda _text: None,
    )

    assert selected is not None
    assert selected.prompt == "authoritative prompt"
    assert selected.paused is False
    assert shutting_down is False
    persisted = load_prompt_queue_snapshot(active.store.directory, active.id)
    assert persisted.prompts == []
    assert persisted.revision == state.queue_revision
