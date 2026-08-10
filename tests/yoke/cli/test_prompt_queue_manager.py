from __future__ import annotations

# ruff: noqa: ANN002,ANN003,ANN202,D100,D103,S101

from yoke.cli.interactive.common import PendingPrompt
from yoke.cli.interactive.queue import manager
from yoke.cli.runtime.terminal_output_gate import (
    is_fullscreen_output_suppressed,
)


def test_queue_manager_defers_turn_output_across_item_edit(
    monkeypatch,
) -> None:
    """Live turn output must not overwrite the queue item editor."""
    manager_runs = 0

    def run_manager(state, *, prompts, changed):
        nonlocal manager_runs
        del prompts, changed
        manager_runs += 1
        assert is_fullscreen_output_suppressed()
        if manager_runs == 1:
            return manager._QueueManagerEditRequest(0)
        return state.prompts

    def edit_prompt(prompt: PendingPrompt) -> PendingPrompt:
        assert is_fullscreen_output_suppressed()
        edited = prompt.copy_for_queue()
        edited.prompt = "edited"
        return edited

    monkeypatch.setattr(manager, "_run_queue_manager", run_manager)

    result = manager.open_queue_manager(
        [PendingPrompt("original")], edit_prompt=edit_prompt
    )

    assert result is not None
    assert [prompt.prompt for prompt in result] == ["edited"]
    assert manager_runs == 2
    assert not is_fullscreen_output_suppressed()


def test_queue_editor_defers_turn_output_when_used_directly(
    monkeypatch,
) -> None:
    """The standalone queue editor must also own terminal output."""

    class FakePromptSession:
        def __init__(self, *, multiline: bool) -> None:
            assert multiline

        def prompt(self, *_args, **_kwargs) -> str:
            assert is_fullscreen_output_suppressed()
            return "edited"

    import prompt_toolkit

    monkeypatch.setattr(prompt_toolkit, "PromptSession", FakePromptSession)

    result = manager.edit_queue_prompt(PendingPrompt("original"))

    assert result is not None
    assert result.prompt == "edited"
    assert not is_fullscreen_output_suppressed()
