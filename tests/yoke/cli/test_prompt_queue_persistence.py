from __future__ import annotations

# ruff: noqa: D100,D103,S101

from pathlib import Path
from threading import Event
from threading import Lock
from threading import Thread
from threading import current_thread

import pytest

from yoke.cli.image_input import ImageAttachment
from yoke.cli.interactive.common import PendingPrompt
from yoke.cli.interactive.common import PromptCliState
from yoke.cli.interactive.prompt.loop import persist_prompt_exit_state
from yoke.cli.interactive.prompt.submission import submit_prompt_toolkit_prompt
from yoke.cli.interactive.prompt.turns import finish_prompt_turn
from yoke.cli.interactive.queue.mutations import append_prompt
from yoke.cli.interactive.queue.mutations import attach_pending_image
from yoke.cli.interactive.queue.mutations import consume_pending_images
from yoke.cli.interactive.queue.mutations import next_pending_prompt_index
from yoke.cli.interactive.queue.mutations import remove_pending_image
from .daemon_queue_support import clear_prompt_queue
from .daemon_queue_support import load_prompt_queue
from .daemon_queue_support import load_prompt_queue_state
from .daemon_queue_support import persist_prompt_queue
from .daemon_queue_support import PromptQueueRevisionConflict
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


def test_consumed_prompt_is_removed_from_persisted_queue(
    active_session_for, tmp_path
) -> None:
    active_session = active_session_for(tmp_path)
    state = PromptCliState(
        messages=[],
        pending_prompts=[
            PendingPrompt("consumed"),
            PendingPrompt("still queued"),
        ],
    )
    persist_prompt_queue(active_session, state.pending_prompts)

    # Only the daemon promotes inputs. Hold the second item for the next pass.
    backend = remote(active_session).backend
    loaded = backend.load_queue()
    backend.connection.session_request(
        "PATCH",
        "/queue",
        json={
            "expectedRevision": loaded.revision,
            "operations": [
                {"op": "setPaused", "id": loaded.prompts[1].id, "paused": True}
            ],
            "resume": False,
        },
    )
    wake(active_session)
    next_prompt, _should_finish = finish_prompt_turn(
        state=state,
        state_lock=Lock(),
        active_session=active_session,
        request_context_usage=lambda _prompt: None,
    )
    restored_prompts, _restored_images = load_prompt_queue(active_session)
    assert next_prompt is None
    assert [prompt.prompt for prompt in state.pending_prompts] == ["still queued"]
    assert [prompt.prompt for prompt in restored_prompts] == ["still queued"]
    assert [m.text_content() for m in backend.messages() if m.role == "user"] == [
        "consumed"
    ]
    loaded = backend.load_queue()
    backend.connection.session_request(
        "PATCH",
        "/queue",
        json={
            "expectedRevision": loaded.revision,
            "operations": [
                {"op": "setPaused", "id": loaded.prompts[0].id, "paused": False}
            ],
        },
    )
    backend.connection.session_request("POST", "/wait", params={"timeoutMs": 5000})
    final_prompt, _should_finish = finish_prompt_turn(
        state=state,
        state_lock=Lock(),
        active_session=active_session,
        request_context_usage=lambda _prompt: None,
    )
    restored_prompts, _restored_images = load_prompt_queue(active_session)
    assert final_prompt is None
    assert restored_prompts == []
    assert [m.text_content() for m in backend.messages() if m.role == "user"] == [
        "consumed",
        "still queued",
    ]


def test_unchanged_prompt_queue_is_not_rewritten(
    active_session_for, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active_session = active_session_for(tmp_path)
    prompts = [PendingPrompt("still queued")]
    persist_prompt_queue(active_session, prompts)
    before = load_prompt_queue_state(active_session).revision
    writes = {"count": 0}
    original_write_text = Path.write_text

    def count_write(
        path: Path,
        data: str,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> int:
        writes["count"] += 1
        return original_write_text(
            path,
            data,
            encoding=encoding,
            errors=errors,
            newline=newline,
        )

    monkeypatch.setattr(Path, "write_text", count_write)

    persist_prompt_queue(active_session, prompts)

    assert writes["count"] == 0
    assert load_prompt_queue_state(active_session).revision == before


def test_clearing_prompt_queue_preserves_monotonic_revision(
    active_session_for, tmp_path: Path
) -> None:
    active_session = active_session_for(tmp_path)
    persist_prompt_queue(active_session, [PendingPrompt("first")])
    before = load_prompt_queue_snapshot(
        store_for(active_session).directory,
        active_session.id,
    )
    assert before.revision == 1

    clear_prompt_queue(active_session)

    cleared = load_prompt_queue_snapshot(
        store_for(active_session).directory,
        active_session.id,
    )
    assert cleared.revision == 2
    assert cleared.prompts == []

    persist_prompt_queue(active_session, [PendingPrompt("second")])
    restored = load_prompt_queue_snapshot(
        store_for(active_session).directory,
        active_session.id,
    )
    assert restored.revision == 3
    assert [item.prompt for item in restored.prompts] == ["second"]


def test_stale_cli_snapshot_does_not_overwrite_newer_disk_edit(
    active_session_for,
    tmp_path: Path,
) -> None:
    active_session = active_session_for(tmp_path)
    original = PendingPrompt("original", id="item-a")
    persist_prompt_queue(active_session, [original])
    loaded = load_prompt_queue_state(active_session)
    edited = (
        load_prompt_queue_snapshot(
            store_for(active_session).directory,
            active_session.id,
        )
        .prompts[0]
        .model_copy(update={"prompt": "edited"})
    )
    _replace_disk_prompts(active_session, [edited])

    with pytest.raises(PromptQueueRevisionConflict) as raised:
        persist_prompt_queue(
            active_session,
            loaded.prompts,
            loaded.pending_images,
            expected_revision=loaded.revision,
        )

    current = load_prompt_queue_snapshot(
        store_for(active_session).directory,
        active_session.id,
    )
    assert raised.value.expected_revision == 1
    assert raised.value.actual_revision == 2
    assert current.revision == 2
    assert [item.prompt for item in current.prompts] == ["edited"]


def test_stale_cli_clear_preserves_new_admission(
    active_session_for, tmp_path: Path
) -> None:
    active_session = active_session_for(tmp_path)
    loaded = load_prompt_queue_state(active_session)
    persist_prompt_queue(
        active_session,
        [PendingPrompt("admitted after CLI read", id="http-item")],
    )

    with pytest.raises(PromptQueueRevisionConflict):
        clear_prompt_queue(active_session, expected_revision=loaded.revision)

    current = load_prompt_queue_snapshot(
        store_for(active_session).directory,
        active_session.id,
    )
    assert current.revision == 1
    assert [item.prompt for item in current.prompts] == ["admitted after CLI read"]


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


def test_persisting_queue_does_not_move_paused_items(
    active_session_for, tmp_path: Path
) -> None:
    active_session = active_session_for(tmp_path)
    prompts = [
        PendingPrompt("active A", id="a"),
        PendingPrompt("paused B", id="b", paused=True),
        PendingPrompt("active C", id="c"),
    ]

    persist_prompt_queue(active_session, prompts)

    current = load_prompt_queue_snapshot(
        store_for(active_session).directory,
        active_session.id,
    )
    assert [item.id for item in current.prompts] == ["a", "b", "c"]


def test_cli_leaves_http_owned_continuations_and_attachments_for_native_runtime(
    active_session_for,
    tmp_path: Path,
) -> None:
    active_session = active_session_for(tmp_path)
    snapshot = PersistedPromptQueue(
        revision=4,
        prompts=[
            PersistedPendingInput(
                id="continuation",
                prompt="",
                continuation=True,
                created_at="2026-09-19T00:00:00+00:00",
            ),
            PersistedPendingInput(
                id="attachment",
                prompt="inspect this",
                attachments=[
                    {
                        "uri": "yoke-upload://upl_test",
                        "name": "notes.txt",
                        "mime": "text/plain",
                    }
                ],
                created_at="2026-09-19T00:00:01+00:00",
            ),
            PersistedPendingInput(
                id="cli",
                prompt="ordinary CLI work",
                created_at="2026-09-19T00:00:02+00:00",
            ),
        ],
    )
    write_prompt_queue_snapshot(
        store_for(active_session).directory,
        active_session.id,
        snapshot,
    )

    loaded = load_prompt_queue_state(active_session)

    assert loaded.prompts[0].continuation is True
    assert loaded.prompts[1].attachments == [
        {"type": "file", **snapshot.prompts[1].attachments[0]}
    ]
    assert next_pending_prompt_index(loaded.prompts) == 2
    persist_prompt_queue(active_session, loaded.prompts)
    current = load_prompt_queue_snapshot(
        store_for(active_session).directory,
        active_session.id,
    )
    assert current.prompts[0].continuation is True
    assert current.prompts[1].attachments == snapshot.prompts[1].attachments


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


def test_pending_image_attach_and_removal_survive_reload(
    active_session_for, tmp_path: Path
) -> None:
    active_session = active_session_for(tmp_path)
    state = _state_from_disk(active_session)
    image = ImageAttachment(path=tmp_path / "queued.png")

    attach_pending_image(
        state=state,
        state_lock=Lock(),
        active_session=active_session,
        attachment=image,
    )

    attached = load_prompt_queue_state(active_session)
    assert attached.pending_images == [image]
    assert attached.revision == 1

    removed = remove_pending_image(
        state=state,
        state_lock=Lock(),
        active_session=active_session,
    )

    assert removed == image
    restored = load_prompt_queue_state(active_session)
    assert restored.pending_images == []
    assert restored.revision == 2


def test_consuming_pending_images_clears_persisted_state(
    active_session_for, tmp_path: Path
) -> None:
    active_session = active_session_for(tmp_path)
    state = _state_from_disk(active_session)
    image = ImageAttachment(path=tmp_path / "submitted.png")
    attach_pending_image(
        state=state,
        state_lock=Lock(),
        active_session=active_session,
        attachment=image,
    )

    consumed = consume_pending_images(
        state=state,
        state_lock=Lock(),
        active_session=active_session,
    )

    assert consumed == [image]
    restored = load_prompt_queue_state(active_session)
    assert restored.pending_images == []
    assert restored.revision == 2


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


def test_append_identity_conflict_preserves_authoritative_item(
    active_session_for, tmp_path: Path
) -> None:
    active_session = active_session_for(tmp_path)
    state = _state_from_disk(active_session)
    persist_prompt_queue(
        active_session,
        [PendingPrompt("authoritative", id="duplicate")],
    )

    appended = append_prompt(
        state=state,
        state_lock=Lock(),
        active_session=active_session,
        prompt=PendingPrompt("new local value", id="duplicate"),
    )

    assert appended is False
    current = load_prompt_queue_snapshot(
        store_for(active_session).directory,
        active_session.id,
    )
    assert current.revision == 1
    assert [item.prompt for item in current.prompts] == ["authoritative"]
    assert "already exists" in state.status_message


def test_cli_appends_serialize_revision_and_state_updates(
    active_session_for,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import yoke.cli.interactive.queue.mutations as mutations

    active_session = active_session_for(tmp_path)
    state = _state_from_disk(active_session)
    first_commit_entered = Event()
    release_first_commit = Event()
    second_lock_requested = Event()
    expected_revisions: list[int] = []
    original_commit = mutations.commit_prompt_queue

    class TrackingLock:
        def __init__(self) -> None:
            self._lock = Lock()

        def __enter__(self):
            if current_thread().name == "second-append":
                second_lock_requested.set()
            self._lock.acquire()
            return self

        def __exit__(self, *_args) -> None:
            self._lock.release()

    def controlled_commit(*args, **kwargs):
        expected_revisions.append(kwargs["expected_revision"])
        prompts = args[1]
        if prompts[-1].prompt == "first":
            first_commit_entered.set()
            assert release_first_commit.wait(timeout=2)
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(mutations, "commit_prompt_queue", controlled_commit)
    state_lock = TrackingLock()
    first = Thread(
        target=append_prompt,
        kwargs={
            "state": state,
            "state_lock": state_lock,
            "active_session": active_session,
            "prompt": PendingPrompt("first", id="first"),
        },
        name="first-append",
    )
    second = Thread(
        target=append_prompt,
        kwargs={
            "state": state,
            "state_lock": state_lock,
            "active_session": active_session,
            "prompt": PendingPrompt("second", id="second"),
        },
        name="second-append",
    )

    first.start()
    assert first_commit_entered.wait(timeout=2)
    second.start()
    assert second_lock_requested.wait(timeout=2)
    assert expected_revisions == [0]
    release_first_commit.set()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert expected_revisions == [0, 1]
    current = load_prompt_queue_snapshot(
        store_for(active_session).directory,
        active_session.id,
    )
    assert current.revision == 2
    assert [item.prompt for item in current.prompts] == ["first", "second"]
    assert [prompt.prompt for prompt in state.pending_prompts] == ["first", "second"]
    assert state.queue_revision == 2


def test_exit_does_not_overwrite_newer_prompt_edit(
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

    persist_prompt_exit_state(
        state=state,
        state_lock=Lock(),
        active_session=active_session,
        agent=remote(active_session).backend,
    )

    current = load_prompt_queue_snapshot(
        store_for(active_session).directory,
        active_session.id,
    )
    assert current.revision == 2
    assert [item.prompt for item in current.prompts] == ["edited"]
