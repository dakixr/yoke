# ruff: noqa: D100, D103, S101

from __future__ import annotations

from yoke.cli.interactive.renderer import PromptToolkitLiveRenderer
from yoke.cli.render.provider_events import format_provider_event


def test_provider_rate_limit_event_formats_retry_wait() -> None:
    text = format_provider_event(
        "provider_rate_limited",
        {
            "provider": "demo",
            "model": "gpt-test",
            "attempt": 3,
            "max_retries": 9,
            "wait_seconds": 61.0,
            "message": "Provider request was rate limited.",
        },
    )

    assert "demo:gpt-test rate limited" in text
    assert "retry 3/9" in text
    assert "waiting 1.0 min" in text


def test_prompt_renderer_emits_provider_warning() -> None:
    warnings: list[str] = []
    statuses: list[str] = []
    tools: list[str] = []
    renderer = PromptToolkitLiveRenderer(
        begin_tool_block=lambda: None,
        emit_tool=lambda text, _failed: tools.append(text),
        emit_agent=lambda _text: None,
        emit_commentary=lambda _text: None,
        emit_error=lambda _text: None,
        emit_notice=lambda _text: None,
        emit_warning=warnings.append,
        set_status=statuses.append,
    )

    renderer.handle_event(
        "provider_rate_limited",
        {
            "provider": "demo",
            "model": "gpt-test",
            "attempt": 1,
            "max_retries": 8,
            "wait_seconds": 3.0,
        },
    )

    assert tools == []
    assert warnings == ["demo:gpt-test rate limited; retry 1/8; waiting 3.0s"]
    assert statuses == ["Rate limited"]
