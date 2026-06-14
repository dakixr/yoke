from __future__ import annotations

from threading import Lock

from yoke.cli.interactive import _format_bottom_toolbar
from yoke.cli.interactive.prompt_rendering import build_prompt_toolbar
from yoke.cli.interactive.common import PromptCliState
from yoke.cli.interactive.prompt_keys import (
    cycle_prompt_thinking_effort,
)


def test_cycle_prompt_thinking_effort_advances_and_wraps() -> None:
    assert cycle_prompt_thinking_effort(None) == "xhigh"
    assert cycle_prompt_thinking_effort("none") == "low"
    assert cycle_prompt_thinking_effort("low") == "medium"
    assert cycle_prompt_thinking_effort("medium") == "high"
    assert cycle_prompt_thinking_effort("high") == "xhigh"
    assert cycle_prompt_thinking_effort("xhigh") == "none"


def test_cycle_prompt_thinking_effort_uses_model_capabilities() -> None:
    assert cycle_prompt_thinking_effort(None, ("high", "max")) == "max"
    assert cycle_prompt_thinking_effort("high", ("high", "max")) == "max"
    assert cycle_prompt_thinking_effort("max", ("high", "max")) == "high"
    assert cycle_prompt_thinking_effort("low", ("thinking",)) == "thinking"


def test_cycle_prompt_thinking_effort_returns_none_without_capabilities() -> None:
    assert cycle_prompt_thinking_effort("high", ()) is None


def test_idle_toolbar_shows_provider_with_effort_before_context() -> None:
    toolbar = _format_bottom_toolbar(
        worker_active=False,
        stop_pending=False,
        status_message="",
        pending_prompts=[],
        provider_model="CodexSubscriptionProvider gpt-5.4 high",
        context_usage="99% left",
        root_label=r"~\dev\ScriptsCommon",
    )

    assert toolbar == [
        (
            "class:bottom-toolbar",
            r" CodexSubscriptionProvider gpt-5.4 high · 99% left · ~\dev\ScriptsCommon ",
        )
    ]


def test_toolbar_reads_provider_model_dynamically() -> None:
    provider_model = {"value": "CodexSubscriptionProvider gpt-5.4 high"}
    toolbar = build_prompt_toolbar(
        state=PromptCliState(messages=[], pending_prompts=[]),
        state_lock=Lock(),
        provider_model_text=lambda: provider_model["value"],
        spinner_frames=("|",),
        root_label=r"~\dev\ScriptsCommon",
    )

    assert (
        r" CodexSubscriptionProvider gpt-5.4 high · ~\dev\ScriptsCommon "
        == toolbar()[0][1]
    )
    provider_model["value"] = "CodexSubscriptionProvider gpt-5.4 xhigh"
    assert (
        r" CodexSubscriptionProvider gpt-5.4 xhigh · ~\dev\ScriptsCommon "
        == toolbar()[0][1]
    )
