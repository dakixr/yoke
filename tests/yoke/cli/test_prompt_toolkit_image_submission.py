from __future__ import annotations

# ruff: noqa: F403, F405
# ruff: noqa: ANN002,ANN003,ANN202,D100,D103,F405,S101

from .support import *  # noqa: F403, F405


def test_process_prompt_toolkit_prompt_preserves_image_reference_text(
    tmp_path: Path,
) -> None:
    from threading import Lock

    from yoke.agent.models import MessageLocalImageContentPart
    from yoke.agent.models import MessageTextContentPart
    from yoke.cli.image_input import ImageAttachment
    from yoke.cli.interactive.common import PromptCliState
    from yoke.cli.interactive.prompt_loop import (
        process_prompt_toolkit_prompt,
    )

    image_path = tmp_path / "yoke-clipboard-wmtfeura.png"
    image_path.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO7Z0mQAAAAASUVORK5CYII="
        )
    )
    agent = ImageAwareAgent()
    state = PromptCliState(
        messages=[],
        pending_prompts=[],
        pending_images=[ImageAttachment(path=image_path)],
        abandoned_turn_ids=set(),
        steered_turn_ids=set(),
    )
    submitted: dict[str, object] = {}

    def start_turn(prompt: str, *, user_message: Message | None = None):
        submitted["prompt"] = prompt
        submitted["user_message"] = user_message
        return None

    active_session = active_session_for(tmp_path)
    updated_session = process_prompt_toolkit_prompt(
        "before [yoke-clipboard-wmtfeura.png] and after",
        state=state,
        agent=agent,
        active_session_ref={"active_session": active_session},
        scrollback_console=build_console(CaptureStream()),
        state_lock=Lock(),
        update_status=lambda _message: None,
        invalidate_prompt=lambda: None,
        request_exit=lambda: None,
        start_turn=start_turn,
        steer_active_turn=lambda *_args, **_kwargs: False,
        format_context_usage_text=lambda _payload: None,
    )

    assert updated_session is active_session
    assert submitted["prompt"] == "before [yoke-clipboard-wmtfeura.png] and after"
    message = submitted["user_message"]
    assert isinstance(message, Message)
    assert message.text_content() == "before [yoke-clipboard-wmtfeura.png] and after"
    assert state.pending_images == []
    content = message.content
    assert isinstance(content, list)
    assert content == [
        MessageTextContentPart(text="before [yoke-clipboard-wmtfeura.png] and after"),
        MessageLocalImageContentPart(
            path=str(image_path.resolve()),
            label="[Image #1]",
        ),
    ]


def test_process_prompt_toolkit_prompt_attaches_dropped_image_path_line(
    tmp_path: Path,
) -> None:
    from threading import Lock

    from yoke.agent.models import MessageLocalImageContentPart
    from yoke.agent.models import MessageTextContentPart
    from yoke.cli.interactive.common import PromptCliState
    from yoke.cli.interactive.prompt_loop import (
        process_prompt_toolkit_prompt,
    )

    image_path = tmp_path / "Screenshot 2026-06-04 at 09.22.53.png"
    image_path.write_bytes(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO7Z0mQAAAAASUVORK5CYII="
        )
    )
    escaped_path = str(image_path).replace(" ", "\\ ")
    state = PromptCliState(
        messages=[],
        pending_prompts=[],
        abandoned_turn_ids=set(),
        steered_turn_ids=set(),
    )
    submitted: dict[str, object] = {}

    def start_turn(prompt: str, *, user_message: Message | None = None):
        submitted["prompt"] = prompt
        submitted["user_message"] = user_message
        return None

    active_session = active_session_for(tmp_path)
    process_prompt_toolkit_prompt(
        f"what is in this screenshot?\n{escaped_path}",
        state=state,
        agent=ImageAwareAgent(),
        active_session_ref={"active_session": active_session},
        scrollback_console=build_console(CaptureStream()),
        state_lock=Lock(),
        update_status=lambda _message: None,
        invalidate_prompt=lambda: None,
        request_exit=lambda: None,
        start_turn=start_turn,
        steer_active_turn=lambda *_args, **_kwargs: False,
        format_context_usage_text=lambda _payload: None,
    )

    assert submitted["prompt"] == (
        "what is in this screenshot?\n[Screenshot 2026-06-04 at 09.22.53.png]"
    )
    message = submitted["user_message"]
    assert isinstance(message, Message)
    assert state.pending_images == []
    content = message.content
    assert isinstance(content, list)
    assert content == [
        MessageTextContentPart(text=submitted["prompt"]),
        MessageLocalImageContentPart(
            path=str(image_path.resolve()),
            label="[Image #1]",
        ),
    ]


def test_process_prompt_toolkit_prompt_starts_compaction_without_blocking(
    tmp_path: Path,
) -> None:
    from threading import Lock
    from threading import Thread

    from yoke.cli.interactive.common import PromptCliState
    from yoke.cli.interactive.prompt_loop import (
        process_prompt_toolkit_prompt,
    )

    state = PromptCliState(
        messages=[],
        pending_prompts=[],
        abandoned_turn_ids=set(),
        steered_turn_ids=set(),
    )
    state_lock = Lock()
    active_session = active_session_for(tmp_path)
    started_compaction = False
    worker = Thread(target=lambda: None)

    def start_compaction():
        nonlocal started_compaction
        started_compaction = True
        with state_lock:
            state.worker = worker
        return worker

    def start_turn(*_args, **_kwargs):
        return Thread(target=lambda: None)

    updated_session = process_prompt_toolkit_prompt(
        "/compact",
        state=state,
        agent=ImageAwareAgent(),
        active_session_ref={"active_session": active_session},
        scrollback_console=build_console(CaptureStream()),
        state_lock=state_lock,
        update_status=lambda _message: None,
        invalidate_prompt=lambda: None,
        request_exit=lambda: None,
        start_turn=start_turn,
        start_compaction=start_compaction,
        steer_active_turn=lambda *_args, **_kwargs: False,
        format_context_usage_text=lambda _payload: None,
    )

    assert updated_session is active_session
    assert started_compaction

    process_prompt_toolkit_prompt(
        "queued while compacting",
        state=state,
        agent=ImageAwareAgent(),
        active_session_ref={"active_session": active_session},
        scrollback_console=build_console(CaptureStream()),
        state_lock=state_lock,
        update_status=lambda _message: None,
        invalidate_prompt=lambda: None,
        request_exit=lambda: None,
        start_turn=start_turn,
        start_compaction=start_compaction,
        steer_active_turn=lambda *_args, **_kwargs: False,
        format_context_usage_text=lambda _payload: None,
    )

    assert [prompt.prompt for prompt in state.pending_prompts] == [
        "queued while compacting"
    ]
