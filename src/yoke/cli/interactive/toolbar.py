"""Prompt-toolkit bottom toolbar formatting."""

from __future__ import annotations

from collections.abc import Sequence

from yoke.cli.interactive.common import PendingPrompt
from yoke.cli.interactive.common import format_pending_summary
from yoke.cli.render import truncate_cli_text
from yoke.cli.render.theme import format_token_count
from yoke.cli.render.theme import show_gauge
from yoke.cli.render.theme import show_timer
from yoke.cli.render.theme import show_tokens
from yoke.cli.render.theme import show_tool_count
from yoke.cli.render.theme import show_turn_number

# The toolbar formatter mirrors yoke's compact fragment construction; many
# fragment literals are intentionally long to keep the visual structure clear.
# ruff: noqa: E501


def format_bottom_toolbar(  # noqa: PLR0913
    *,
    worker_active: bool,
    stop_pending: bool,
    status_message: str,
    pending_prompts: Sequence[str | PendingPrompt],
    pending_images: Sequence[str] = (),
    context_usage: str | None = None,
    context_usage_percent: int | None = None,
    context_input_tokens: int | None = None,
    context_max_tokens: int | None = None,
    provider_model: str | None = None,
    root_label: str | None = None,
    session_title: str | None = None,
    spinner_frame: str | None = None,
    columns: int | None = None,
    turn_elapsed_seconds: float | None = None,
    turn_tool_count: int = 0,
    turn_input_tokens: int | None = None,
    turn_output_tokens: int | None = None,
    turn_reasoning_tokens: int | None = None,
    turn_number: int | None = None,
) -> list[tuple[str, str]]:
    """Format the prompt-toolkit toolbar as styled fragments."""
    fragments: list[tuple[str, str]] = []
    line_fragments: list[tuple[str, str]] = []
    _append_pending_lines(fragments, pending_prompts, pending_images)
    _append_status(
        line_fragments,
        worker_active=worker_active,
        stop_pending=stop_pending,
        status_message=status_message,
        pending_prompts=pending_prompts,
        spinner_frame=spinner_frame,
    )
    if worker_active and not stop_pending:
        _append_metrics(
            line_fragments,
            turn_number=turn_number,
            turn_input_tokens=turn_input_tokens,
            turn_output_tokens=turn_output_tokens,
            turn_reasoning_tokens=turn_reasoning_tokens,
            turn_elapsed_seconds=turn_elapsed_seconds,
            turn_tool_count=turn_tool_count,
        )
    _append_context(
        line_fragments,
        worker_active=worker_active,
        stop_pending=stop_pending,
        context_usage=context_usage,
        context_usage_percent=context_usage_percent,
        context_input_tokens=context_input_tokens,
        context_max_tokens=context_max_tokens,
    )
    _append_identity(
        line_fragments, worker_active, stop_pending, provider_model, root_label
    )
    _append_title(line_fragments, session_title, columns)
    line_fragments.append(("", " "))
    if not line_fragments:
        line_fragments.append(("", " "))
    fragments.extend(
        (style or "class:bottom-toolbar", text) for style, text in line_fragments
    )
    return fragments


def _append_pending_lines(fragments, pending_prompts, pending_images) -> None:
    for index, prompt in enumerate(pending_prompts, start=1):
        if isinstance(prompt, PendingPrompt):
            prompt_text = prompt.prompt
            prompt_label = "steering" if prompt.kind == "steering" else "queued"
        else:
            prompt_text = prompt
            prompt_label = "queued"
        fragments.append(
            (
                "class:bottom-toolbar.queue",
                f" {prompt_label} {index}: {truncate_cli_text(prompt_text, 72)} \n",
            )
        )
    for image_line in pending_images:
        fragments.append(("class:bottom-toolbar", f"{image_line}\n"))


def _append_status(
    line_fragments,
    *,
    worker_active,
    stop_pending,
    status_message,
    pending_prompts,
    spinner_frame,
) -> None:
    if worker_active:
        if stop_pending:
            cancel_status = (
                status_message
                if status_message.startswith("Cancelling ")
                else "Cancelling model request"
            )
            line_fragments.append(
                ("class:bottom-toolbar.cancel", f" {cancel_status}...")
            )
        else:
            frame = spinner_frame or "⠋"
            line_fragments.append(("class:bottom-toolbar.spinner", f" {frame} "))
            line_fragments.append(
                ("class:bottom-toolbar.status", status_message or "Thinking")
            )
            pending_summary = format_pending_summary(pending_prompts)
            if pending_summary:
                line_fragments.append(("class:bottom-toolbar.queue", pending_summary))
    elif pending_prompts:
        pending_summary = format_pending_summary(pending_prompts)
        line_fragments.append(("", pending_summary.removeprefix(" · ")))


def _append_metrics(
    line_fragments,
    *,
    turn_number,
    turn_input_tokens,
    turn_output_tokens,
    turn_reasoning_tokens,
    turn_elapsed_seconds,
    turn_tool_count,
) -> None:
    if turn_number is not None and show_turn_number():
        _metric_sep(line_fragments)
        line_fragments.append(("class:bottom-toolbar.timer", f"#{turn_number}"))
    if show_tokens() and (
        turn_input_tokens is not None or turn_output_tokens is not None
    ):
        _metric_sep(line_fragments)
        parts = []
        if turn_input_tokens is not None:
            parts.append(f"↓{format_token_count(turn_input_tokens)}")
        if turn_output_tokens is not None:
            parts.append(f"↑{format_token_count(turn_output_tokens)}")
        if turn_reasoning_tokens is not None and turn_reasoning_tokens > 0:
            parts.append(f"⚡{format_token_count(turn_reasoning_tokens)}")
        line_fragments.append(("class:bottom-toolbar.tokens", " ".join(parts)))
    if show_timer() and turn_elapsed_seconds is not None:
        _metric_sep(line_fragments)
        line_fragments.append(
            (
                "class:bottom-toolbar.timer",
                _format_elapsed(turn_elapsed_seconds),
            )
        )
    if show_tool_count() and turn_tool_count > 0:
        _metric_sep(line_fragments)
        label = "tool" if turn_tool_count == 1 else "tools"
        line_fragments.append(
            ("class:bottom-toolbar.tools", f"{turn_tool_count} {label}")
        )


def _append_context(
    line_fragments,
    *,
    worker_active,
    stop_pending,
    context_usage,
    context_usage_percent,
    context_input_tokens,
    context_max_tokens,
) -> None:
    if not show_gauge() or (context_usage_percent is None and not context_usage):
        return
    _toolbar_sep(line_fragments, worker_active, stop_pending)
    line_fragments.append(("class:bottom-toolbar.gauge.text", context_usage or ""))
    if (
        context_usage_percent is not None
        and context_input_tokens is not None
        and context_max_tokens is not None
        and show_tokens()
    ):
        line_fragments.append(
            (
                "class:bottom-toolbar.gauge.text",
                f" ({format_token_count(context_input_tokens)}/{format_token_count(context_max_tokens)})",
            )
        )


def _append_identity(
    line_fragments, worker_active, stop_pending, provider_model, root_label
) -> None:
    identity_parts = [part for part in [provider_model, root_label] if part]
    if not identity_parts:
        return
    _toolbar_sep(line_fragments, worker_active, stop_pending)
    line_fragments.append(("class:bottom-toolbar.identity", " · ".join(identity_parts)))


def _append_title(line_fragments, session_title, columns) -> None:
    title = " ".join((session_title or "").split())
    left_text = "".join(text for _style, text in line_fragments)
    left_width = _text_width(left_text)
    if title and columns is not None and columns > 0:
        available = columns - left_width - 3
        if available >= 8:
            title_text = _truncate_display_text(title, available)
            if title_text:
                padding = max(2, columns - left_width - _text_width(title_text) - 1)
                line_fragments.append(("", " " * padding))
                line_fragments.append(("class:bottom-toolbar.title", title_text))


def _metric_sep(line_fragments) -> None:
    line_fragments.append(("", " · " if line_fragments else " "))


def _toolbar_sep(line_fragments, worker_active, stop_pending) -> None:
    if line_fragments:
        line_fragments.append(("", " · "))
    else:
        line_fragments.append(("", " "))


def _format_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = int(seconds // 60)
    remaining = int(seconds % 60)
    return f"{minutes}m{remaining:02d}s"


def _truncate_display_text(text: str, max_width: int) -> str:
    if _text_width(text) <= max_width:
        return text
    if max_width <= 3:
        return ""
    result = ""
    for char in text:
        next_result = f"{result}{char}"
        if _text_width(f"{next_result}...") > max_width:
            break
        result = next_result
    return f"{result.rstrip()}..."


def _text_width(text: str) -> int:
    try:
        from prompt_toolkit.formatted_text.utils import fragment_list_width
    except ImportError:
        return len(text)
    return fragment_list_width([("", text)])
