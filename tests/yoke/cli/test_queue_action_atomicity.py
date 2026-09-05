from __future__ import annotations

# ruff: noqa: D100,D103,S101

import base64
from pathlib import Path
from threading import Event, Lock, Thread

import pytest

from yoke.cli.image_input import ImageAttachment
from yoke.cli.interactive.common import PendingPrompt, PromptCliState
from yoke.cli.interactive.common import format_context_usage_text
from yoke.cli.interactive.prompt.loop import process_prompt_toolkit_prompt
from yoke.cli.interactive.prompt.submission import submit_prompt_toolkit_prompt
from yoke.cli.interactive.queue import manager
from yoke.cli.interactive.queue.mutations import attach_pending_image
from yoke.cli.interactive.queue.persistence import load_prompt_queue_state
from yoke.cli.interactive.queue.persistence import persist_prompt_queue
from yoke.cli.render import build_console
from yoke.session.queue import load_prompt_queue_snapshot

from .support import CaptureStream, FakeAgent, active_session_for


def _state_from_disk(active_session) -> PromptCliState:
    loaded = load_prompt_queue_state(active_session)
    return PromptCliState(
        messages=[],
        pending_prompts=loaded.prompts,
        pending_images=loaded.pending_images,
        queue_revision=loaded.revision,
        queue_session_id=active_session.id,
    )


def _tiny_png(path: Path) -> ImageAttachment:
    path.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/"
            "x8AAwMCAO7Z0mQAAAAASUVORK5CYII="
        )
    )
    return ImageAttachment(path=path)


def _attach(active_session, state, attachment: ImageAttachment) -> None:
    attach_pending_image(
        state=state,
        state_lock=Lock(),
        active_session=active_session,
        attachment=attachment,
    )


def _process_queue_manager(
    active_session,
    state,
    *,
    stdout: CaptureStream | None = None,
    start_turn=lambda *_args, **_kwargs: Thread(),
    start_pending_prompt=None,
    steer_active_turn=lambda *_args, **_kwargs: False,
):
    return process_prompt_toolkit_prompt(
        "/queue",
        state=state,
        agent=FakeAgent(),
        active_session_ref={"active_session": active_session},
        scrollback_console=build_console(stdout or CaptureStream()),
        state_lock=Lock(),
        update_status=lambda _message: None,
        invalidate_prompt=lambda: None,
        request_exit=lambda: None,
        start_turn=start_turn,
        start_pending_prompt=start_pending_prompt,
        steer_active_turn=steer_active_turn,
        format_context_usage_text=format_context_usage_text,
    )


def test_missing_image_does_not_consume_the_attachment_or_prompt(
    tmp_path: Path,
) -> None:
    active = active_session_for(tmp_path)
    state = _state_from_disk(active)
    image = ImageAttachment(tmp_path / "missing.png")
    _attach(active, state, image)

    with pytest.raises(FileNotFoundError):
        submit_prompt_toolkit_prompt(
            "describe it",
            action="queue",
            state=state,
            active_session=active,
            state_lock=Lock(),
            invalidate_prompt=lambda: None,
            start_turn=lambda *_args, **_kwargs: pytest.fail("prompt started"),
            steer_active_turn=lambda *_args, **_kwargs: False,
        )

    persisted = load_prompt_queue_state(active)
    assert persisted.pending_images == [image]
    assert persisted.prompts == []
    assert state.next_editor_text == "describe it"


def test_failing_message_builder_leaves_images_and_prompt_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = active_session_for(tmp_path)
    state = _state_from_disk(active)
    image = _tiny_png(tmp_path / "image.png")
    _attach(active, state, image)
    monkeypatch.setattr(
        "yoke.cli.interactive.prompt.submission.build_user_message",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("builder failed")),
    )

    with pytest.raises(RuntimeError, match="builder failed"):
        submit_prompt_toolkit_prompt(
            "describe it",
            action="queue",
            state=state,
            active_session=active,
            state_lock=Lock(),
            invalidate_prompt=lambda: None,
            start_turn=lambda *_args, **_kwargs: pytest.fail("prompt started"),
            steer_active_turn=lambda *_args, **_kwargs: False,
        )

    persisted = load_prompt_queue_state(active)
    assert persisted.pending_images == [image]
    assert persisted.prompts == []
    assert state.next_editor_text == "describe it"


def test_queue_append_and_image_consumption_use_one_commit(tmp_path: Path) -> None:
    active = active_session_for(tmp_path)
    state = _state_from_disk(active)
    image = _tiny_png(tmp_path / "image.png")
    _attach(active, state, image)
    state.worker = Thread()

    submit_prompt_toolkit_prompt(
        "describe it",
        action="queue",
        state=state,
        active_session=active,
        state_lock=Lock(),
        invalidate_prompt=lambda: None,
        start_turn=lambda *_args, **_kwargs: pytest.fail("prompt started"),
        steer_active_turn=lambda *_args, **_kwargs: False,
    )

    persisted = load_prompt_queue_snapshot(active.store.directory, active.id)
    assert persisted.revision == 2
    assert persisted.pending_images == []
    assert [item.prompt for item in persisted.prompts] == ["describe it"]
    assert persisted.prompts[0].user_message is not None
    assert persisted.prompts[0].user_message.has_image_inputs()


def test_failing_queue_commit_retains_prompt_and_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yoke.cli.interactive.queue.mutations as mutations

    active = active_session_for(tmp_path)
    state = _state_from_disk(active)
    image = _tiny_png(tmp_path / "image.png")
    _attach(active, state, image)
    state.worker = Thread()
    monkeypatch.setattr(
        mutations,
        "commit_prompt_queue",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        submit_prompt_toolkit_prompt(
            "describe it",
            action="queue",
            state=state,
            active_session=active,
            state_lock=Lock(),
            invalidate_prompt=lambda: None,
            start_turn=lambda *_args, **_kwargs: pytest.fail("prompt started"),
            steer_active_turn=lambda *_args, **_kwargs: False,
        )

    persisted = load_prompt_queue_state(active)
    assert persisted.pending_images == [image]
    assert persisted.prompts == []
    assert state.next_editor_text == "describe it"


def test_failing_immediate_start_retains_prompt_and_images(tmp_path: Path) -> None:
    active = active_session_for(tmp_path)
    state = _state_from_disk(active)
    image = _tiny_png(tmp_path / "image.png")
    _attach(active, state, image)

    with pytest.raises(RuntimeError, match="start failed"):
        submit_prompt_toolkit_prompt(
            "describe it",
            action="steer",
            state=state,
            active_session=active,
            state_lock=Lock(),
            invalidate_prompt=lambda: None,
            start_turn=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("start failed")
            ),
            steer_active_turn=lambda *_args, **_kwargs: False,
        )

    persisted = load_prompt_queue_state(active)
    assert persisted.pending_images == [image]
    assert persisted.prompts == []
    assert state.next_editor_text == "describe it"


def test_failing_post_start_image_commit_does_not_retry_accepted_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yoke.cli.interactive.queue.images as image_mutations

    active = active_session_for(tmp_path)
    state = _state_from_disk(active)
    image = _tiny_png(tmp_path / "image.png")
    _attach(active, state, image)
    started: list[str] = []

    def start(prompt: str, **_kwargs) -> Thread:
        started.append(prompt)
        return Thread()

    monkeypatch.setattr(
        image_mutations,
        "commit_prompt_queue",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )

    with pytest.raises(OSError, match="disk full"):
        submit_prompt_toolkit_prompt(
            "describe it",
            action="steer",
            state=state,
            active_session=active,
            state_lock=Lock(),
            invalidate_prompt=lambda: None,
            start_turn=start,
            steer_active_turn=lambda *_args, **_kwargs: False,
        )

    persisted = load_prompt_queue_state(active)
    assert started == ["describe it"]
    assert persisted.pending_images == [image]
    assert persisted.prompts == []
    assert state.next_editor_text is None


def test_failing_steer_callback_retains_prompt_and_images(tmp_path: Path) -> None:
    active = active_session_for(tmp_path)
    state = _state_from_disk(active)
    image = _tiny_png(tmp_path / "image.png")
    _attach(active, state, image)
    state.worker = Thread()

    with pytest.raises(RuntimeError, match="steer failed"):
        submit_prompt_toolkit_prompt(
            "describe it",
            action="steer",
            state=state,
            active_session=active,
            state_lock=Lock(),
            invalidate_prompt=lambda: None,
            start_turn=lambda *_args, **_kwargs: pytest.fail("prompt started"),
            steer_active_turn=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("steer failed")
            ),
        )

    persisted = load_prompt_queue_state(active)
    assert persisted.pending_images == [image]
    assert persisted.prompts == []
    assert state.next_editor_text == "describe it"


def test_steering_false_queues_once_with_its_image(tmp_path: Path) -> None:
    active = active_session_for(tmp_path)
    state = _state_from_disk(active)
    image = _tiny_png(tmp_path / "image.png")
    _attach(active, state, image)
    state.worker = Thread()
    steering_calls = 0

    def lose_worker_race(*_args, **_kwargs) -> bool:
        nonlocal steering_calls
        steering_calls += 1
        state.worker = None
        return False

    submit_prompt_toolkit_prompt(
        "describe it",
        action="steer",
        state=state,
        active_session=active,
        state_lock=Lock(),
        invalidate_prompt=lambda: None,
        start_turn=lambda *_args, **_kwargs: pytest.fail("prompt started twice"),
        steer_active_turn=lose_worker_race,
    )

    persisted = load_prompt_queue_state(active)
    assert steering_calls == 1
    assert persisted.pending_images == []
    assert [item.prompt for item in persisted.prompts] == ["describe it"]


def test_manager_restores_dequeued_steering_when_worker_race_is_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = active_session_for(tmp_path)
    prompt = PendingPrompt("steer me", kind="steering", id="steer")
    persist_prompt_queue(active, [prompt])
    state = _state_from_disk(active)
    state.worker = Thread()
    state.active_stop_request = Event()
    monkeypatch.setattr(
        manager,
        "_run_queue_manager",
        lambda manager_state, **_kwargs: manager_state.prompts,
    )

    _process_queue_manager(active, state, steer_active_turn=lambda *_args: False)

    persisted = load_prompt_queue_state(active)
    assert [item.id for item in persisted.prompts] == ["steer"]
    assert [item.id for item in state.pending_prompts] == ["steer"]


def test_unchanged_idle_manager_does_not_start_ordinary_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = active_session_for(tmp_path)
    persist_prompt_queue(active, [PendingPrompt("ordinary", id="ordinary")])
    state = _state_from_disk(active)
    monkeypatch.setattr(
        manager,
        "_run_queue_manager",
        lambda manager_state, **_kwargs: manager_state.prompts,
    )

    _process_queue_manager(
        active,
        state,
        start_turn=lambda *_args, **_kwargs: pytest.fail("ordinary prompt started"),
        start_pending_prompt=lambda *_args: pytest.fail("ordinary prompt started"),
    )

    persisted = load_prompt_queue_state(active)
    assert [item.id for item in persisted.prompts] == ["ordinary"]
    assert persisted.revision == 1


def test_manager_restores_steering_when_start_callback_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active = active_session_for(tmp_path)
    persist_prompt_queue(
        active,
        [PendingPrompt("steer me", kind="steering", id="steer")],
    )
    state = _state_from_disk(active)
    monkeypatch.setattr(
        manager,
        "_run_queue_manager",
        lambda manager_state, **_kwargs: manager_state.prompts,
    )

    with pytest.raises(RuntimeError, match="start failed"):
        _process_queue_manager(
            active,
            state,
            start_turn=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("start failed")
            ),
        )

    persisted = load_prompt_queue_state(active)
    assert [item.id for item in persisted.prompts] == ["steer"]


def test_stale_empty_submit_does_not_bypass_authoritative_queue(tmp_path: Path) -> None:
    active = active_session_for(tmp_path)
    state = _state_from_disk(active)
    persist_prompt_queue(active, [PendingPrompt("first", id="first")])

    submit_prompt_toolkit_prompt(
        "second",
        action="queue",
        state=state,
        active_session=active,
        state_lock=Lock(),
        invalidate_prompt=lambda: None,
        start_turn=lambda *_args, **_kwargs: pytest.fail("second bypassed first"),
        steer_active_turn=lambda *_args, **_kwargs: False,
    )

    persisted = load_prompt_queue_state(active)
    assert [item.prompt for item in persisted.prompts] == ["first", "second"]


@pytest.mark.parametrize("command", ["/new", "/fork"])
def test_active_worker_rejects_session_switch(tmp_path: Path, command: str) -> None:
    active = active_session_for(tmp_path)
    state = _state_from_disk(active)
    state.worker = Thread()
    active_ref = {"active_session": active}
    stdout = CaptureStream()

    result = process_prompt_toolkit_prompt(
        command,
        state=state,
        agent=FakeAgent(),
        active_session_ref=active_ref,
        scrollback_console=build_console(stdout),
        state_lock=Lock(),
        update_status=lambda _message: None,
        invalidate_prompt=lambda: None,
        request_exit=lambda: None,
        start_turn=lambda *_args, **_kwargs: Thread(),
        steer_active_turn=lambda *_args, **_kwargs: False,
        format_context_usage_text=format_context_usage_text,
    )

    assert result is active
    assert active_ref["active_session"] is active
    assert "Finish or stop the active turn" in stdout.getvalue()


def test_worker_completion_after_switch_rejection_does_not_create_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yoke.cli.interactive.prompt.loop as prompt_loop

    active = active_session_for(tmp_path)
    state = _state_from_disk(active)
    state.worker = Thread()
    active_ref = {"active_session": active}
    notice_entered = Event()
    release_notice = Event()
    results = []

    def controlled_notice(*_args, **_kwargs) -> None:
        notice_entered.set()
        assert release_notice.wait(timeout=2)

    monkeypatch.setattr(prompt_loop, "print_scrollback_notice", controlled_notice)

    def switch() -> None:
        results.append(
            process_prompt_toolkit_prompt(
                "/new",
                state=state,
                agent=FakeAgent(),
                active_session_ref=active_ref,
                scrollback_console=build_console(CaptureStream()),
                state_lock=Lock(),
                update_status=lambda _message: None,
                invalidate_prompt=lambda: None,
                request_exit=lambda: None,
                start_turn=lambda *_args, **_kwargs: Thread(),
                steer_active_turn=lambda *_args, **_kwargs: False,
                format_context_usage_text=format_context_usage_text,
            )
        )

    command = Thread(target=switch)
    command.start()
    assert notice_entered.wait(timeout=2)
    state.worker = None
    release_notice.set()
    command.join(timeout=2)

    assert not command.is_alive()
    assert results == [active]
    assert active_ref["active_session"] is active
