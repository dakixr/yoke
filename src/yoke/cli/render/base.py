"""Rendering helpers for yoke CLI output."""

from __future__ import annotations

import json
import os
import textwrap
from typing import Protocol
from typing import TextIO
from typing import cast

from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text

from yoke._version import __version__


def truncate_cli_text(text: str, limit: int) -> str:
    """Normalize and truncate text for compact CLI output."""
    normalized = " ".join(text.split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 3].rstrip() + "..."


def parse_tool_arguments(raw_arguments: object) -> dict[str, object]:
    """Parse raw tool arguments into a dict when possible."""
    if isinstance(raw_arguments, dict):
        if not all(isinstance(key, str) for key in raw_arguments):
            return {}
        return cast(dict[str, object], raw_arguments)
    if not isinstance(raw_arguments, str):
        return {}
    try:
        parsed = json.loads(raw_arguments)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    if not all(isinstance(key, str) for key in parsed):
        return {}
    return cast(dict[str, object], parsed)


def format_tool_argument_value(value: object, *, limit: int = 72) -> str:
    """Format one tool argument preview value."""
    if isinstance(value, str):
        return json.dumps(truncate_cli_text(value, limit))
    try:
        serialized = json.dumps(value, separators=(",", ":"), default=str)
    except TypeError:
        serialized = str(value)
    return truncate_cli_text(serialized, limit)


def format_tool_arguments_preview(raw_arguments: object) -> str:
    """Format a compact preview of tool arguments."""
    arguments = parse_tool_arguments(raw_arguments)
    if not arguments:
        if isinstance(raw_arguments, str) and raw_arguments.strip():
            return truncate_cli_text(raw_arguments, 160)
        return ""
    parts = [
        f"{key}={format_tool_argument_value(value)}" for key, value in arguments.items()
    ]
    return truncate_cli_text(" ".join(parts), 220)


def format_tool_preview(tool_name: str, raw_arguments: object) -> str:
    """Format a tool name plus preview arguments."""
    preview = format_tool_arguments_preview(raw_arguments)
    if preview:
        return f"{tool_name} {preview}"
    return tool_name


def format_compaction_note(payload: dict[str, object]) -> str:
    """Format a context compaction event line."""
    reason = payload.get("reason")
    if reason == "threshold":
        prefix = "auto context compaction"
    elif reason == "overflow_retry":
        prefix = "context overflow retry compaction"
    else:
        prefix = "context compaction"
    before = payload.get("input_tokens")
    after = payload.get("compacted_input_tokens")
    if isinstance(before, int) and isinstance(after, int):
        return (
            f"{prefix} (from {_format_token_count(before)} tokens "
            f"to {_format_token_count(after)} tokens)"
        )
    return prefix


def _format_token_count(tokens: int) -> str:
    if tokens < 1_000:
        return str(tokens)
    thousands = tokens / 1_000
    if tokens % 1_000 == 0 or thousands >= 10:
        return f"{round(thousands):.0f}k"
    return f"{thousands:.1f}k"


def format_user_separator(prompt: str) -> str:
    """Format the plain-text user separator."""
    return f"---\nuser:\n{prompt.rstrip()}\n---"


class OutputStream(Protocol):
    """Output stream protocol."""

    def write(self, text: str, /) -> object:
        """Write text to the stream."""

    def flush(self) -> object:
        """Flush the stream."""

    def isatty(self) -> bool:
        """Return whether the stream is a TTY."""
        ...


def build_console(stream: OutputStream) -> Console:
    """Create a Rich console for the given output stream."""
    is_tty = stream.isatty()
    return Console(
        file=cast(TextIO, stream),
        force_terminal=is_tty,
        color_system="standard" if is_tty else None,
        no_color=False,
        highlight=False,
    )


def print_version_banner(console: Console) -> None:
    """Print the yoke version banner in dim styling."""
    console.print(Text(f"Version {__version__}", style="dim"))


def print_user_prompt(console: Console, prompt: str) -> None:
    """Print a user prompt in interactive mode."""
    prompt = _sanitize_console_output(console, prompt.rstrip())
    if console.is_terminal and _supports_console_chrome(console):
        console.print(_user_prompt_block(console, prompt))
        return
    console.print(format_user_separator(prompt), markup=False)


def _user_prompt_block(console: Console, prompt: str) -> Text:
    width = max(1, console.width)
    content_width = width
    content_lines = prompt.splitlines() or [""]
    wrapped_lines: list[str] = []
    for line in content_lines:
        wrapped = textwrap.wrap(
            line,
            width=content_width,
            replace_whitespace=False,
            drop_whitespace=False,
        )
        wrapped_lines.extend(wrapped or [""])
    block_lines: list[str] = [" " * width]
    for line in wrapped_lines:
        block_lines.append(line.ljust(content_width))
    block_lines.append(" " * width)
    return Text("\n".join(block_lines), style="bold bright_white on grey23")


def print_agent_output(console: Console, output: str) -> None:
    """Print final agent output with rich Markdown rendering."""
    sanitized = _sanitize_console_output(console, output.rstrip() or "(empty)")
    if console.is_terminal:
        console.print(Markdown(sanitized))
    else:
        console.print(sanitized)


def print_error(console: Console, message: str) -> None:
    """Print an error message."""
    message = _sanitize_console_output(console, message)
    if console.is_terminal:
        console.print(f"[bold red]Error:[/bold red] {message}")
        return
    console.print(f"Error: {message}")


def _sanitize_console_output(console: Console, text: str) -> str:
    return _sanitize_text_for_encoding(text, encoding=console.encoding)


def _supports_console_chrome(console: Console) -> bool:
    return _can_encode_text("â”€â”‚â”Œâ”â””â”˜", encoding=console.encoding)


def _sanitize_text_for_encoding(text: str, *, encoding: str | None) -> str:
    active_encoding = encoding
    if not active_encoding:
        try:
            active_encoding = os.device_encoding(1)
        except OSError:
            active_encoding = None
    active_encoding = active_encoding or "utf-8"
    if _can_encode_text(text, encoding=active_encoding):
        return text
    return text.encode(active_encoding, errors="replace").decode(active_encoding)


def _can_encode_text(text: str, *, encoding: str | None) -> bool:
    active_encoding = encoding
    if not active_encoding:
        try:
            active_encoding = os.device_encoding(1)
        except OSError:
            active_encoding = None
    active_encoding = active_encoding or "utf-8"
    try:
        text.encode(active_encoding)
        return True
    except UnicodeEncodeError:
        return False
