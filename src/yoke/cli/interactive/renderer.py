"""Prompt-toolkit renderers and toolbar helpers."""

from __future__ import annotations

from collections.abc import Callable
from collections.abc import Sequence
from typing import cast

from yoke.cli.interactive.common import PendingPrompt
from yoke.cli.interactive.common import format_context_usage_text
from yoke.cli.interactive.common import format_pending_summary
from yoke.cli.render import format_compaction_note
from yoke.cli.render import format_tool_preview
from yoke.cli.render import truncate_cli_text


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
        self._record_tool_event = record_tool_event
        self._tool_divider_emitted = False
        self._turn_has_tool_output = False

    def __enter__(self) -> PromptToolkitLiveRenderer:
        """Enter the renderer context."""
        self._tool_divider_emitted = False
        self._turn_has_tool_output = False
        self._set_status("Thinking")
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        """Exit the renderer context."""
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
            return
        if event == "context_compaction":
            self._emit_notice(format_compaction_note(payload))
            self._set_status("Thinking")
            return
        if event == "model_start":
            self._set_status("Thinking")
            return
        if event == "model_end":
            self._set_status("Planning next step")
            return
        if event == "assistant_message":
            if payload.get("phase") == "commentary":
                self._ensure_tool_block()
                content = payload.get("content")
                if isinstance(content, str) and content.strip():
                    self._emit_commentary(content.strip())
                self._set_status("Working")
            return
        if event == "tool_execution_start":
            self._ensure_tool_block()
            tool_name = payload.get("tool_name", "tool")
            tool_arguments = payload.get("tool_arguments")
            self._emit_tool_output(
                format_tool_preview(str(tool_name), tool_arguments), False
            )
            self._set_status("Waiting on tool result")
            return
        if event == "tool_execution_end":
            if not payload.get("ok", False):
                self._emit_tool_output(
                    _tool_error_text(payload) or "The tool returned an error.",
                    True,
                )
                self._set_status("Handling tool failure")
                return
            self._set_status("Thinking")

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
    provider_model: str | None = None,
    root_label: str | None = None,
    session_title: str | None = None,
    spinner_frame: str | None = None,
    columns: int | None = None,
) -> list[tuple[str, str]]:
    """Format the prompt-toolkit toolbar lines."""
    lines: list[str] = []
    for index, prompt in enumerate(pending_prompts, start=1):
        if isinstance(prompt, PendingPrompt):
            prompt_text = prompt.prompt
            prompt_label = "steering" if prompt.kind == "steering" else "queued"
        else:
            prompt_text = prompt
            prompt_label = "queued"
        lines.append(f" {prompt_label} {index}: {truncate_cli_text(prompt_text, 72)} ")
    lines.extend(pending_images)
    core_parts = [
        part
        for part in [
            provider_model,
            context_usage,
            root_label,
        ]
        if part
    ]
    core = " · ".join(core_parts)
    right = f" · {core}" if core else ""
    if worker_active:
        if stop_pending:
            pending_status = (
                status_message
                if status_message.startswith("Cancelling ")
                else "Cancelling model request"
            )
            lines.append(
                _format_toolbar_line(
                    f" {pending_status}...{right} ",
                    session_title=session_title,
                    columns=columns,
                )
            )
        else:
            frame = spinner_frame or "⠋"
            pending_summary = format_pending_summary(pending_prompts)
            status = f" {frame} {status_message}{pending_summary}"
            lines.append(
                _format_toolbar_line(
                    f"{status}{right} ",
                    session_title=session_title,
                    columns=columns,
                )
            )
    elif pending_prompts:
        pending_summary = format_pending_summary(pending_prompts)
        lines.append(
            _format_toolbar_line(
                f" {pending_summary.removeprefix(' · ')}{right} ",
                session_title=session_title,
                columns=columns,
            )
        )
    else:
        lines.append(
            _format_toolbar_line(
                f" {core} ",
                session_title=session_title,
                columns=columns,
            )
        )
    return [("class:bottom-toolbar", "\n".join(lines))]


def _format_toolbar_line(
    left: str,
    *,
    session_title: str | None,
    columns: int | None,
) -> str:
    title = " ".join((session_title or "").split())
    if not title or columns is None or columns <= 0:
        return left
    left_width = _text_width(left)
    available = columns - left_width - 3
    if available < 8:
        return left
    title_text = _truncate_display_text(title, available)
    if not title_text:
        return left
    padding = max(2, columns - left_width - _text_width(title_text) - 1)
    return f"{left}{' ' * padding}{title_text} "


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
