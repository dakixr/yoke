from __future__ import annotations

# ruff: noqa: D100,D103,S101

from pathlib import Path
from threading import Lock
from threading import Thread

import pytest

from yoke.cli.image_input import ImageAttachment
from yoke.cli.interactive.common import PendingPrompt
from yoke.cli.interactive.common import PromptCliState
from yoke.cli.interactive.prompt.submission import submit_prompt_toolkit_prompt
from yoke.cli.interactive.prompt.turns import finish_prompt_turn
from yoke.cli.interactive.queue.mutations import attach_pending_image
from yoke.cli.interactive.queue.mutations import consume_pending_images
from yoke.cli.interactive.queue.mutations import remove_pending_image
from .daemon_queue_support import load_prompt_queue_state
from .daemon_queue_support import persist_prompt_queue
from yoke.session.queue import load_prompt_queue_snapshot
from yoke.session.queue import load_prompt_queue_snapshots
from yoke.session.queue import PersistedPendingInput
from yoke.session.queue import PersistedPromptQueue
from yoke.session.queue import write_prompt_queue_snapshot

from .daemon_queue_support import (
    active_session_for as active_session_for,
    terminal as terminal,
    store_for,
    remote,
    wake,
)


def _state_from_disk(active_session) -> PromptCliState:
    loaded = load_prompt_queue_state(active_session)
    return PromptCliState(
        messages=[],
        pending_prompts=loaded.prompts,
        pending_images=loaded.pending_images,
        queue_revision=loaded.revision,
        queue_session_id=active_session.id,
    )


def _replace_disk_prompts(active_session, prompts: list[PersistedPendingInput]) -> None:
    loaded = load_prompt_queue_state(active_session)
    persist_prompt_queue(
        active_session,
        [
            PendingPrompt(
                item.prompt,
                id=item.id,
                paused=item.paused,
                kind=item.kind,
                continuation=item.continuation,
                attachments=item.attachments,
            )
            for item in prompts
        ],
        loaded.pending_images,
        expected_revision=loaded.revision,
    )


def test_batch_queue_load_reads_each_session_from_one_snapshot(
    tmp_path: Path,
) -> None:
    session_directory = tmp_path / "sessions"
    write_prompt_queue_snapshot(
        session_directory,
        "queued",
        PersistedPromptQueue(revision=7),
    )

    snapshots = load_prompt_queue_snapshots(
        session_directory,
        ["missing", "queued", "queued"],
    )

    assert set(snapshots) == {"missing", "queued"}
    assert snapshots["missing"].revision == 0
    assert snapshots["queued"].revision == 7


def test_cli_append_reapplies_only_new_item_after_disk_edit(
    active_session_for, tmp_path: Path
) -> None:
    active_session = active_session_for(tmp_path)
    persist_prompt_queue(
        active_session,
        [PendingPrompt("original", id="item-a")],
    )
    state = _state_from_disk(active_session)
    state.worker = Thread()
    edited = (
        load_prompt_queue_snapshot(
            store_for(active_session).directory,
            active_session.id,
        )
        .prompts[0]
        .model_copy(update={"prompt": "edited"})
    )
    _replace_disk_prompts(active_session, [edited])

    submit_prompt_toolkit_prompt(
        "new CLI item",
        action="queue",
        state=state,
        active_session=active_session,
        state_lock=Lock(),
        invalidate_prompt=lambda: None,
        start_turn=lambda *_args, **_kwargs: pytest.fail("queued item started"),
        steer_active_turn=lambda *_args, **_kwargs: False,
    )

    current = load_prompt_queue_snapshot(
        store_for(active_session).directory,
        active_session.id,
    )
    assert current.revision == 3
    assert [item.prompt for item in current.prompts] == ["edited", "new CLI item"]
    assert [prompt.prompt for prompt in state.pending_prompts] == [
        "edited",
        "new CLI item",
    ]
    assert state.queue_revision == 3


@pytest.mark.parametrize(
    ("disk_prompt", "paused", "expected_started"),
    [
        ("edited", False, "edited"),
        ("paused remotely", True, None),
        (None, False, None),
    ],
)
def test_finish_turn_dequeues_only_authoritative_item(
    active_session_for,
    tmp_path: Path,
    disk_prompt: str | None,
    paused: bool,
    expected_started: str | None,
) -> None:
    active_session = active_session_for(tmp_path)
    persist_prompt_queue(
        active_session,
        [PendingPrompt("stale local value", id="item-a")],
    )
    state = _state_from_disk(active_session)
    current = load_prompt_queue_snapshot(
        store_for(active_session).directory,
        active_session.id,
    )
    prompts = []
    if disk_prompt is not None:
        prompts = [
            current.prompts[0].model_copy(
                update={"prompt": disk_prompt, "paused": paused}
            )
        ]
    _replace_disk_prompts(active_session, prompts)

    if expected_started is not None:
        wake(active_session)
    next_prompt, should_finish = finish_prompt_turn(
        state=state,
        state_lock=Lock(),
        active_session=active_session,
        request_context_usage=lambda _prompt: None,
    )

    assert should_finish is False
    assert next_prompt is None
    assert [
        m.text_content()
        for m in remote(active_session).backend.messages()
        if m.role == "user"
    ] == ([expected_started] if expected_started else [])
    disk = load_prompt_queue_snapshot(
        store_for(active_session).directory,
        active_session.id,
    )
    if expected_started is None:
        assert disk.revision == 2
        assert [item.prompt for item in disk.prompts] == (
            [disk_prompt] if disk_prompt is not None else []
        )
    else:
        assert disk.revision == 3
        assert disk.prompts == []


def test_image_only_update_preserves_newer_prompt_edit(
    active_session_for, tmp_path: Path
) -> None:
    active_session = active_session_for(tmp_path)
    persist_prompt_queue(
        active_session,
        [PendingPrompt("original", id="item-a")],
    )
    state = _state_from_disk(active_session)
    edited = (
        load_prompt_queue_snapshot(
            store_for(active_session).directory,
            active_session.id,
        )
        .prompts[0]
        .model_copy(update={"prompt": "edited"})
    )
    _replace_disk_prompts(active_session, [edited])
    image = ImageAttachment(path=tmp_path / "queued.png")

    attach_pending_image(
        state=state,
        state_lock=Lock(),
        active_session=active_session,
        attachment=image,
    )

    current = load_prompt_queue_snapshot(
        store_for(active_session).directory,
        active_session.id,
    )
    assert [item.prompt for item in current.prompts] == ["edited"]
    assert current.pending_images == [str(image.path)]
    assert current.revision == 3


@pytest.mark.parametrize("action", ["consume", "remove"])
def test_stale_empty_image_action_refreshes_authoritative_images(
    active_session_for, tmp_path: Path, action: str
) -> None:
    active_session = active_session_for(tmp_path)
    stale_state = _state_from_disk(active_session)
    writer_state = _state_from_disk(active_session)
    image = ImageAttachment(path=tmp_path / "submitted.png")
    attach_pending_image(
        state=writer_state,
        state_lock=Lock(),
        active_session=active_session,
        attachment=image,
    )

    if action == "consume":
        changed = consume_pending_images(
            state=stale_state,
            state_lock=Lock(),
            active_session=active_session,
        )
    else:
        changed = remove_pending_image(
            state=stale_state,
            state_lock=Lock(),
            active_session=active_session,
        )

    expected = [image] if action == "consume" else image
    assert changed == expected
    restored = load_prompt_queue_state(active_session)
    assert restored.pending_images == []
    assert restored.revision == 2
