from __future__ import annotations

# ruff: noqa: F403, F405
# ruff: noqa: ANN002,ANN003,D100,D103,F403,F405,S101

from .support import *  # noqa: F403, F405
from .support import _format_bottom_toolbar  # noqa: F401


def test_gauge_level_thresholds() -> None:
    from yoke.cli.render.theme import gauge_level

    assert gauge_level(0) == "low"
    assert gauge_level(69) == "low"
    assert gauge_level(70) == "mid"
    assert gauge_level(89) == "mid"
    assert gauge_level(90) == "high"
    assert gauge_level(100) == "high"


def test_gauge_style_returns_style_class() -> None:
    from yoke.cli.render.theme import gauge_style

    assert "low" in gauge_style(50)
    assert "mid" in gauge_style(75)
    assert "high" in gauge_style(95)


def test_format_token_count_compact() -> None:
    from yoke.cli.render.theme import format_token_count

    assert format_token_count(0) == "0"
    assert format_token_count(999) == "999"
    assert format_token_count(1_000) == "1k"
    assert format_token_count(1_500) == "1.5k"
    assert format_token_count(10_000) == "10k"
    assert format_token_count(18_342) == "18k"


def test_toolbar_hides_turn_number_by_default() -> None:
    toolbar = _format_bottom_toolbar(
        worker_active=True,
        stop_pending=False,
        status_message="Thinking",
        pending_prompts=[],
        spinner_frame="⠋",
        turn_number=7,
        turn_elapsed_seconds=12.0,
    )

    text = "".join(t for _s, t in toolbar)
    assert "#7" not in text
    assert "12s" in text


def test_toolbar_shows_tool_count_when_active() -> None:
    toolbar = _format_bottom_toolbar(
        worker_active=True,
        stop_pending=False,
        status_message="Thinking",
        pending_prompts=[],
        spinner_frame="⠋",
        turn_tool_count=3,
    )

    text = "".join(t for _s, t in toolbar)
    assert "3 tools" in text


def test_toolbar_shows_single_tool_label() -> None:
    toolbar = _format_bottom_toolbar(
        worker_active=True,
        stop_pending=False,
        status_message="Thinking",
        pending_prompts=[],
        spinner_frame="⠋",
        turn_tool_count=1,
    )

    text = "".join(t for _s, t in toolbar)
    assert "1 tool" in text


def test_toolbar_hides_token_counts_by_default() -> None:
    toolbar = _format_bottom_toolbar(
        worker_active=True,
        stop_pending=False,
        status_message="Thinking",
        pending_prompts=[],
        spinner_frame="⠋",
        turn_input_tokens=18_000,
        turn_output_tokens=1_200,
        turn_reasoning_tokens=500,
    )

    text = "".join(t for _s, t in toolbar)
    assert "↓18k" not in text
    assert "↑1.2k" not in text
    assert "⚡500" not in text


def test_toolbar_shows_context_gauge_without_absolute_tokens_by_default() -> None:
    toolbar = _format_bottom_toolbar(
        worker_active=False,
        stop_pending=False,
        status_message="",
        pending_prompts=[],
        context_usage="80% left",
        context_usage_percent=20,
        context_input_tokens=40_000,
        context_max_tokens=200_000,
    )

    text = "".join(t for _s, t in toolbar)
    assert "80% left" in text
    assert "40k/200k" not in text


def test_toolbar_hides_turn_metrics_when_idle() -> None:
    toolbar = _format_bottom_toolbar(
        worker_active=False,
        stop_pending=False,
        status_message="",
        pending_prompts=[],
        turn_number=5,
        turn_elapsed_seconds=30.0,
        turn_tool_count=2,
        turn_input_tokens=10_000,
    )

    text = "".join(t for _s, t in toolbar)
    assert "#5" not in text
    assert "30s" not in text
    assert "2 tools" not in text
    assert "↓10k" not in text


def test_toolbar_shows_cancel_status_with_ellipsis() -> None:
    toolbar = _format_bottom_toolbar(
        worker_active=True,
        stop_pending=True,
        status_message="Thinking",
        pending_prompts=[],
    )

    text = "".join(t for _s, t in toolbar)
    assert "Cancelling model request..." in text


def test_phase_strings_are_short_gerunds() -> None:
    from yoke.cli.render.theme import (
        PHASE_COMPACTING,
        PHASE_RECOVERING,
        PHASE_RUNNING_TOOL,
        PHASE_STREAMING,
        PHASE_THINKING,
    )

    for phase in (
        PHASE_THINKING,
        PHASE_STREAMING,
        PHASE_RUNNING_TOOL,
        PHASE_COMPACTING,
        PHASE_RECOVERING,
    ):
        assert len(phase) <= 15
