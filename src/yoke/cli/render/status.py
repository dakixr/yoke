"""Renderer classes for yoke CLI output."""

from __future__ import annotations

import os
from threading import Lock
from typing import cast

from rich.status import Status
from rich.text import Text

from yoke.cli.render.base import OutputStream
from yoke.cli.render.base import build_console
from yoke.cli.render.base import format_compaction_note
from yoke.cli.render.base import format_tool_preview
from yoke.cli.render.base import print_version_banner
from yoke.cli.render.base import truncate_cli_text
from yoke.cli.render.scrollback import (
    print_scrollback_commentary,
)
from yoke.cli.render.scrollback import print_scrollback_notice
from yoke.cli.render.scrollback import print_tool_response_divider


class StatusIndicator:
    """Status indicator for headless and standard CLI turns."""

    def __init__(self, stream: OutputStream, *, animate: bool = True) -> None:
        self._animate = animate
        self._console = build_console(stream)
        self._status: Status | None = None
        self._last_message = ""
        self._enabled = not bool(os.environ.get("YOKE_HEADLESS"))
        self._lock = Lock()
        self._turn_has_tool_output = False

    def __enter__(self) -> StatusIndicator:
        """Start the active status indicator when appropriate."""
        with self._lock:
            self._turn_has_tool_output = False
            if self._enabled and self._animate and self._console.is_terminal:
                self._status = self._console.status(
                    "[bold cyan]Thinking[/bold cyan]", spinner="dots"
                )
                self._status.start()
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        """Stop the active status indicator."""
        self.clear()
        return None

    def handle_event(self, event: str, payload: dict[str, object]) -> None:
        """Handle one agent runtime event."""
        if event == "compaction_summary_start":
            if not self._turn_has_tool_output:
                self._log_blank_line()
            tokens = payload.get("estimated_input_tokens", "?")
            self._log_event_line(
                "compact",
                f"summarizing {tokens} input tokens\u2026",
                style="yellow",
            )
            self._turn_has_tool_output = True
            self._update("Compacting")
            return
        if event == "compaction_summary_end":
            ok = payload.get("ok", False)
            duration = payload.get("duration_seconds", "?")
            if ok:
                chars = payload.get("response_chars", "?")
                self._log_event_line(
                    "compact",
                    f"\u2713 done in {duration}s (summary: {chars} chars)",
                    style="yellow",
                )
            else:
                error = payload.get("error", "unknown")
                self._log_event_line(
                    "compact",
                    f"\u2717 failed after {duration}s ({error})",
                    style="yellow",
                )
            self._update("Thinking")
            return
        if event == "context_compaction":
            self._log_event_line(
                "Note",
                format_compaction_note(payload),
                style="yellow",
            )
            self._update("Thinking")
            return
        if event == "model_start":
            self._update("Thinking")
            return
        if event == "model_end":
            self._update("Planning next step")
            return
        if event == "assistant_message":
            if payload.get("phase") == "commentary":
                content = payload.get("content")
                if isinstance(content, str) and content.strip():
                    self._log_commentary(content.strip())
                self._update("Working")
            return
        if event == "tool_execution_start":
            if not self._turn_has_tool_output:
                self._log_blank_line()
            tool_name = payload.get("tool_name", "tool")
            tool_arguments = payload.get("tool_arguments")
            self._log_event_line(
                format_tool_preview(str(tool_name), tool_arguments),
                style="dim",
            )
            self._turn_has_tool_output = True
            self._update("Waiting on tool result")
            return
        if event == "tool_execution_end":
            ok = payload.get("ok", False)
            if not ok:
                self._log_event_line(
                    _tool_error_text(payload) or "The tool returned an error.",
                    style="dim",
                )
            self._update("Thinking" if ok else "Handling tool failure")

    def clear(self) -> None:
        """Clear the active status line."""
        with self._lock:
            if self._status is not None:
                self._status.stop()
                self._status = None

    def _update(self, message: str) -> None:
        with self._lock:
            self._last_message = message
            if self._status is not None:
                self._status.update(f"[bold cyan]{message}[/bold cyan]")

    def _log_blank_line(self) -> None:
        with self._lock:
            if not self._enabled or not self._console.is_terminal:
                return
            had_status = self._status is not None
            if had_status and self._status is not None:
                self._status.stop()
            self._console.print()
            if had_status and self._animate:
                self._status = self._console.status(
                    f"[bold cyan]{self._last_message}[/bold cyan]",
                    spinner="dots",
                )
                self._status.start()

    def _log_event_line(
        self, label_or_text: str, text: str | None = None, *, style: str
    ) -> None:
        with self._lock:
            if not self._enabled or not self._console.is_terminal:
                return
            had_status = self._status is not None
            if had_status and self._status is not None:
                self._status.stop()
            rendered = label_or_text if text is None else f"{label_or_text} {text}"
            self._console.print(Text(rendered, style=style))
            if had_status and self._animate:
                self._status = self._console.status(
                    f"[bold cyan]{self._last_message}[/bold cyan]",
                    spinner="dots",
                )
                self._status.start()

    def _log_commentary(self, text: str) -> None:
        with self._lock:
            if not self._enabled or not self._console.is_terminal:
                return
            had_status = self._status is not None
            if had_status and self._status is not None:
                self._status.stop()
            print_scrollback_commentary(self._console, text)
            if had_status and self._animate:
                self._status = self._console.status(
                    f"[bold cyan]{self._last_message}[/bold cyan]",
                    spinner="dots",
                )
                self._status.start()


class InteractiveRenderer:
    """Renderer for the basic interactive CLI loop."""

    def __init__(self, stream: OutputStream) -> None:
        self._console = build_console(stream)
        self._lock = Lock()
        self._turn_has_tool_output = False

    def __enter__(self) -> InteractiveRenderer:
        """Enter the renderer context."""
        self._turn_has_tool_output = False
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        """Exit the renderer context."""
        return None

    def handle_event(self, event: str, payload: dict[str, object]) -> None:
        """Handle one agent runtime event."""
        if event == "compaction_summary_start":
            tokens = payload.get("estimated_input_tokens", "?")
            self._print_labeled_line(
                "compact",
                f"summarizing {tokens} input tokens\u2026",
                style="yellow",
            )
            return
        if event == "compaction_summary_end":
            ok = payload.get("ok", False)
            duration = payload.get("duration_seconds", "?")
            if ok:
                chars = payload.get("response_chars", "?")
                self._print_labeled_line(
                    "compact",
                    f"\u2713 done in {duration}s (summary: {chars} chars)",
                    style="yellow",
                )
            else:
                error = payload.get("error", "unknown")
                self._print_labeled_line(
                    "compact",
                    f"\u2717 failed after {duration}s ({error})",
                    style="yellow",
                )
            return
        if event == "context_compaction":
            print_scrollback_notice(self._console, format_compaction_note(payload))
            return
        if event in {"model_start", "model_end"}:
            return
        if event == "assistant_message":
            if payload.get("phase") == "commentary":
                content = payload.get("content")
                if isinstance(content, str) and content.strip():
                    self.print_commentary(content.strip())
            return
        if event == "tool_execution_start":
            if not self._turn_has_tool_output:
                self._console.print()
            tool_name = payload.get("tool_name", "tool")
            tool_arguments = payload.get("tool_arguments")
            self._print_event_line(format_tool_preview(str(tool_name), tool_arguments))
            self._turn_has_tool_output = True
            return
        if event == "tool_execution_end" and not payload.get("ok", False):
            self._print_event_line(
                _tool_error_text(payload) or "The tool returned an error."
            )
            self._turn_has_tool_output = True

    def print_intro(self) -> None:
        """Print the interactive intro banner."""
        print_version_banner(self._console)
        self._console.print(
            Text(
                "yoke interactive mode. Type `exit` or `quit` to leave. "
                "Press `Esc` twice to stop the current turn.\n"
                "Use Ctrl+J for a new line.\n",
                style="dim",
            )
        )

    def print_agent_output(self, text: str) -> None:
        """Print a complete assistant block."""
        with self._lock:
            if self._turn_has_tool_output:
                print_tool_response_divider(self._console)
                self._turn_has_tool_output = False
            self._console.print(text or "(empty)")

    def print_commentary(self, text: str) -> None:
        """Print assistant commentary without labels or dividers."""
        with self._lock:
            print_scrollback_commentary(self._console, text)

    def print_error(self, message: str) -> None:
        """Print an error line."""
        self._print_labeled_line("error", message, style="red")

    def _print_block(self, label: str, text: str, *, style: str) -> None:
        with self._lock:
            lines = (text.rstrip() or "(empty)").splitlines() or ["(empty)"]
            self._print_labeled_line(label, lines[0], style=style)
            padding = " " * (len(label) + 2)
            for line in lines[1:]:
                self._console.print(f"{padding}{line}")

    def _print_labeled_line(self, label: str, text: str, *, style: str) -> None:
        with self._lock:
            self._console.print(Text(f"{label:>5} {text}", style=style))

    def _print_event_line(self, text: str, *, style: str = "dim") -> None:
        with self._lock:
            self._console.print(Text(text, style=style))


def _tool_error_text(payload: dict[str, object]) -> str | None:
    result = payload.get("result")
    if not isinstance(result, dict) or not all(isinstance(key, str) for key in result):
        return None
    result_dict = cast(dict[str, object], result)
    raw_error = result_dict.get("error")
    if isinstance(raw_error, str) and raw_error.strip():
        return truncate_cli_text(raw_error, 120)
    return None
