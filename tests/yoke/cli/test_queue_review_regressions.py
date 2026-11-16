"""Queue revision checks must also cover stale mirrors with no runnable item."""

from __future__ import annotations

from pathlib import Path
from threading import Lock

import pytest

from yoke.cli.interactive.common import PendingPrompt, PromptCliState
from yoke.cli.interactive.prompt.turns import finish_prompt_turn
from .daemon_queue_support import load_prompt_queue_state, persist_prompt_queue
from yoke.session.queue import (
    load_prompt_queue_snapshot,
)

from .daemon_queue_support import (
    active_session_for as active_session_for,
    terminal as terminal,
    store_for,
    remote,
    wake,
)


@pytest.mark.parametrize("initially_paused", [False, True])
def test_finish_turn_refreshes_an_empty_or_paused_mirror(
    active_session_for, tmp_path: Path, initially_paused: bool
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
    persist_prompt_queue(active, [PendingPrompt("authoritative prompt", id="item")])
    wake(active)

    selected, shutting_down = finish_prompt_turn(
        state=state,
        state_lock=Lock(),
        active_session=active,
        request_context_usage=lambda _text: None,
    )

    assert selected is None
    assert [
        m.text_content() for m in remote(active).backend.messages() if m.role == "user"
    ] == ["authoritative prompt"]
    assert state.pending_prompts == []
    assert shutting_down is False
    persisted = load_prompt_queue_snapshot(store_for(active).directory, active.id)
    assert persisted.prompts == []
    assert persisted.revision == state.queue_revision
