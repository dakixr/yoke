from __future__ import annotations

# ruff: noqa: ANN002,ANN003,ANN202,D100,D103,S101

from yoke.cli.interactive.common import PendingPrompt
from yoke.cli.interactive.prompt.rendering import run_scrollback_render
from yoke.cli.interactive.queue import manager
from yoke.cli.runtime.terminal_output_gate import (
    defer_until_fullscreen_exits,
)
from yoke.cli.runtime.terminal_output_gate import (
    is_fullscreen_output_suppressed,
)
from yoke.cli.runtime.terminal_output_gate import (
    suppress_terminal_output_for_fullscreen,
)


def test_queue_manager_defers_turn_output_across_item_edit(
    monkeypatch,
) -> None:
    """Live turn output must not overwrite the queue item editor."""
    emitted: list[str] = []
    manager_runs = 0

    def run_manager(state, *, prompts, changed):
        nonlocal manager_runs
        del prompts, changed
        manager_runs += 1
        assert is_fullscreen_output_suppressed()
        current_run = manager_runs
        assert defer_until_fullscreen_exits(
            lambda: emitted.append(f"manager-{current_run}")
        )
        assert emitted == []
        if manager_runs == 1:
            return manager._QueueManagerEditRequest(0)
        return state.prompts

    def edit_prompt(prompt: PendingPrompt) -> PendingPrompt:
        assert is_fullscreen_output_suppressed()
        assert defer_until_fullscreen_exits(lambda: emitted.append("during-edit"))
        assert emitted == []
        edited = prompt.copy_for_queue()
        edited.prompt = "edited"
        return edited

    monkeypatch.setattr(manager, "_run_queue_manager", run_manager)

    result = manager.open_queue_manager(
        [PendingPrompt("original")], edit_prompt=edit_prompt
    )

    assert result is not None
    assert [prompt.prompt for prompt in result] == ["edited"]
    assert emitted == ["manager-1", "during-edit", "manager-2"]
    assert not is_fullscreen_output_suppressed()


def test_queue_editor_defers_turn_output_when_used_directly(
    monkeypatch,
) -> None:
    """The standalone queue editor must also own terminal output."""
    emitted: list[str] = []

    class FakePromptSession:
        def __init__(self, *, multiline: bool) -> None:
            assert multiline

        def prompt(self, *_args, **_kwargs) -> str:
            assert is_fullscreen_output_suppressed()
            assert defer_until_fullscreen_exits(lambda: emitted.append("tool output"))
            assert emitted == []
            return "edited"

    import prompt_toolkit

    monkeypatch.setattr(prompt_toolkit, "PromptSession", FakePromptSession)

    result = manager.edit_queue_prompt(PendingPrompt("original"))

    assert result is not None
    assert result.prompt == "edited"
    assert emitted == ["tool output"]
    assert not is_fullscreen_output_suppressed()


def test_already_scheduled_scrollback_render_rechecks_output_gate() -> None:
    """A callback queued before opening the editor must not print into it."""
    emitted: list[str] = []

    with suppress_terminal_output_for_fullscreen():
        run_scrollback_render(
            loop=object(),
            render=lambda: emitted.append("assistant output"),
            run_in_terminal=lambda callback: callback(),
        )
        assert emitted == []

    assert emitted == ["assistant output"]
