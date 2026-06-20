"""Prompt-toolkit renderers and toolbar helpers."""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Sequence
from typing import cast

from yoke.cli.interactive.common import PendingPrompt
from yoke.cli.interactive.common import format_context_usage_text
from yoke.cli.interactive.common import format_pending_summary
from yoke.cli.interactive.common import parse_context_usage_details
from yoke.cli.render import format_compaction_note
from yoke.cli.render import format_tool_preview
from yoke.cli.render import truncate_cli_text
from yoke.cli.render.theme import format_token_count
from yoke.cli.render.theme import show_gauge
from yoke.cli.render.theme import show_timer
from yoke.cli.render.theme import show_tokens
from yoke.cli.render.theme import show_tool_count
from yoke.cli.render.theme import show_turn_number


class PromptToolkitLiveRenderer:
    """Renderer for prompt-toolkit interactive sessions."""

    def __init__(
        self,
        *,
        begin_tool_block: Callable[[], None],
        emit_tool: Callable[[str, bool], None],
        emit_agent: Callable[[str], None],
        emit_commentary: Callable[[str], None],
        emit_error: Callable[[str], None],
        emit_notice: Callable[[str], None],
        set_status: Callable[[str], None],
        emit_tool_response_divider: Callable[[], None] | None = None,
        set_context_usage: Callable[[str | None], None] | None = None,
        set_context_details: Callable[[dict[str, int | None]], None] | None = None,
        set_turn_tokens: Callable[[dict[str, int | None]], None] | None = None,
        increment_tool_count: Callable[[], None] | None = None,
        emit_turn_summary: Callable[[dict[str, object]], None] | None = None,
        record_tool_event: Callable[[str, dict[str, object]], None] | None = None,
    ) -> None:
        self._begin_tool_block = begin_tool_block
        self._emit_tool_response_divider = emit_tool_response_divider
        self._emit_tool = emit_tool
        self._emit_agent = emit_agent
        self._emit_commentary = emit_commentary
        self._emit_error = emit_error
        self._emit_notice = emit_notice
        self._set_status = set_status
        self._set_context_usage = set_context_usage
        self._set_context_details = set_context_details
        self._set_turn_tokens = set_turn_tokens
        self._increment_tool_count = increment_tool_count
        self._emit_turn_summary = emit_turn_summary
        self._record_tool_event = record_tool_event
        self._tool_divider_emitted = False
        self._turn_has_tool_output = False
        self._turn_tool_count = 0
        self._turn_input_tokens: int | None = None
        self._turn_output_tokens: int | None = None
        self._turn_reasoning_tokens: int | None = None

    def __enter__(self) -> PromptToolkitLiveRenderer:
        """Enter the renderer context."""
        self._tool_divider_emitted = False
        self._turn_has_tool_output = False
        self._turn_tool_count = 0
        self._turn_input_tokens = None
        self._turn_output_tokens = None
        self._turn_reasoning_tokens = None
        self._set_status("Thinking")
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        """Exit the renderer context and emit per-turn summary."""
        self._tool_divider_emitted = False
        self._set_status("")
        return None

    def _ensure_tool_block(self) -> None:
        """Emit the tool-call divider once for the current turn."""
        if self._tool_divider_emitted:
            return
        self._begin_tool_block()
        self._tool_divider_emitted = True

    def handle_event(self, event: str, payload: dict[str, object]) -> None:
        """Handle one runtime event."""
        self._record_tool_event_if_needed(event, payload)
        if event == "compaction_summary_start":
            tokens = payload.get("estimated_input_tokens", "?")
            self._ensure_tool_block()
            self._emit_tool_output(
                f"compact: summarizing {tokens} input tokens\u2026", False
            )
            self._set_status("Compacting")
            return
        if event == "compaction_summary_end":
            ok = payload.get("ok", False)
            duration = payload.get("duration_seconds", "?")
            if ok:
                chars = payload.get("response_chars", "?")
                self._emit_tool_output(
                    f"compact: \u2713 {duration}s ({chars} chars)",
                    False,
                )
            else:
                error = payload.get("error", "unknown")
                self._emit_tool_output(
                    f"compact: \u2717 failed after {duration}s ({error})", True
                )
            self._set_status("Thinking")
            return
        if event == "context_usage":
            if self._set_context_usage is not None:
                self._set_context_usage(format_context_usage_text(payload))
            if self._set_context_details is not None:
                self._set_context_details(parse_context_usage_details(payload))
            self._capture_turn_tokens(payload)
            return
        if event == "context_compaction":
            self._emit_notice(format_compaction_note(payload))
            self._set_status("Thinking")
            return
        if event == "model_start":
            self._set_status("Thinking")
            return
        if event == "model_end":
            self._set_status("Streaming")
            return
        if event == "assistant_message":
            if payload.get("phase") == "commentary":
                self._ensure_tool_block()
                content = payload.get("content")
                if isinstance(content, str) and content.strip():
                    self._emit_commentary(content.strip())
                self._set_status("Streaming")
            return
        if event == "tool_execution_start":
            self._ensure_tool_block()
            tool_name = payload.get("tool_name", "tool")
            tool_arguments = payload.get("tool_arguments")
            self._emit_tool_output(
                format_tool_preview(str(tool_name), tool_arguments), False
            )
            self._turn_tool_count += 1
            if self._increment_tool_count is not None:
                self._increment_tool_count()
            self._set_status("Running tool")
            return
        if event == "tool_execution_end":
            if not payload.get("ok", False):
                self._emit_tool_output(
                    _tool_error_text(payload) or "The tool returned an error.",
                    True,
                )
                self._set_status("Recovering")
                return
            self._set_status("Thinking")

    def _capture_turn_tokens(self, payload: dict[str, object]) -> None:
        """Extract per-turn and cumulative token counts from a usage payload."""
        input_tokens = payload.get("input_tokens")
        output_tokens = payload.get("output_tokens")
        reasoning_tokens = payload.get("reasoning_tokens")
        if isinstance(input_tokens, int):
            self._turn_input_tokens = input_tokens
        if isinstance(output_tokens, int):
            self._turn_output_tokens = output_tokens
        if isinstance(reasoning_tokens, int):
            self._turn_reasoning_tokens = reasoning_tokens
        if self._set_turn_tokens is not None:
            self._set_turn_tokens(
                {
                    "input_tokens": self._turn_input_tokens,
                    "output_tokens": self._turn_output_tokens,
                    "reasoning_tokens": self._turn_reasoning_tokens,
                }
            )

    def print_agent_output(self, text: str) -> None:
        """Emit assistant output."""
        if self._turn_has_tool_output:
            if self._emit_tool_response_divider is not None:
                self._emit_tool_response_divider()
        self._emit_agent(text)
        self._turn_has_tool_output = False

    def print_error(self, message: str) -> None:
        """Emit an error line."""
        self._emit_error(message)

    def _emit_tool_output(self, text: str, failed: bool) -> None:
        if not self._turn_has_tool_output:
            self._emit_tool("", False)
        self._emit_tool(text, failed)
        self._turn_has_tool_output = True

    def _record_tool_event_if_needed(
        self,
        event: str,
        payload: dict[str, object],
    ) -> None:
        if event not in {
            "tool_execution_start",
            "tool_execution_output_delta",
            "tool_execution_end",
        }:
            return
        if self._record_tool_event is not None:
            self._record_tool_event(event, payload)


def format_bottom_toolbar(
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

    def _sep() -> None:
        line_fragments.append(("", " · "))

    # Queue/pending lines come first as separate toolbar lines.
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

    # --- Build the main status line (all on one line) ---
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

    # --- Live turn metrics (only while worker is active) ---
    if worker_active and not stop_pending:

        def _metric_sep() -> None:
            if not line_fragments:
                line_fragments.append(("", " "))
            else:
                _sep()

        if turn_number is not None and show_turn_number():
            _metric_sep()
            line_fragments.append(("class:bottom-toolbar.timer", f"#{turn_number}"))

        if show_tokens() and (
            turn_input_tokens is not None or turn_output_tokens is not None
        ):
            _metric_sep()
            token_parts: list[str] = []
            if turn_input_tokens is not None:
                token_parts.append(f"\u2193{format_token_count(turn_input_tokens)}")
            if turn_output_tokens is not None:
                token_parts.append(f"\u2191{format_token_count(turn_output_tokens)}")
            if turn_reasoning_tokens is not None and turn_reasoning_tokens > 0:
                token_parts.append(f"\u26a1{format_token_count(turn_reasoning_tokens)}")
            line_fragments.append(
                ("class:bottom-toolbar.tokens", " ".join(token_parts))
            )

        if show_timer() and turn_elapsed_seconds is not None:
            _metric_sep()
            line_fragments.append(
                ("class:bottom-toolbar.timer", _format_elapsed(turn_elapsed_seconds))
            )

        if show_tool_count() and turn_tool_count > 0:
            _metric_sep()
            tool_label = "tool" if turn_tool_count == 1 else "tools"
            line_fragments.append(
                ("class:bottom-toolbar.tools", f"{turn_tool_count} {tool_label}")
            )

    # --- Context gauge ---
    if show_gauge() and (context_usage_percent is not None or context_usage):
        if context_usage_percent is not None:
            if line_fragments:
                if worker_active and not stop_pending:
                    line_fragments.append(("", " · "))
                else:
                    _sep()
            else:
                line_fragments.append(("", " "))
            gauge_label = context_usage or ""
            if gauge_label.strip():
                line_fragments.append(("class:bottom-toolbar.gauge.text", gauge_label))
            if (
                context_input_tokens is not None
                and context_max_tokens is not None
                and show_tokens()
            ):
                line_fragments.append(
                    (
                        "class:bottom-toolbar.gauge.text",
                        f" ({format_token_count(context_input_tokens)}"
                        f"/{format_token_count(context_max_tokens)})",
                    )
                )
        else:
            if line_fragments:
                if worker_active and not stop_pending:
                    line_fragments.append(("", " · "))
                else:
                    _sep()
            else:
                line_fragments.append(("", " "))
            line_fragments.append(
                ("class:bottom-toolbar.gauge.text", context_usage or "")
            )

    # --- Identity line (model · root) ---
    identity_parts = [part for part in [provider_model, root_label] if part]
    if identity_parts:
        identity_text = " · ".join(identity_parts)
        if line_fragments:
            if worker_active and not stop_pending:
                line_fragments.append(("", " · "))
            else:
                _sep()
        else:
            line_fragments.append(("", " "))
        line_fragments.append(("class:bottom-toolbar.identity", identity_text))

    # Right-align session title
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

    line_fragments.append(("", " "))

    # Fallback: empty toolbar gets a single space.
    if not line_fragments:
        line_fragments.append(("", " "))

    # Flush the main line into the fragment list.
    for style, text in line_fragments:
        fragments.append((style or "class:bottom-toolbar", text))

    return fragments


def _format_elapsed(seconds: float) -> str:
    """Format elapsed seconds compactly for the toolbar."""
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


def _tool_error_text(payload: dict[str, object]) -> str | None:
    result = payload.get("result")
    if not isinstance(result, dict) or not all(isinstance(key, str) for key in result):
        return None
    result_dict = cast(dict[str, object], result)
    raw_error = result_dict.get("error")
    if isinstance(raw_error, str) and raw_error.strip():
        return truncate_cli_text(raw_error, 120)
    return None
