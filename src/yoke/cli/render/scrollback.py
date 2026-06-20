"""Scrollback rendering helpers for yoke CLI output."""

from __future__ import annotations

import json
import textwrap

from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text

from yoke.agent.models import Message
from yoke.agent.prompting import parse_memory_message
from yoke.cli.render.base import format_tool_preview
from yoke.cli.render.base import truncate_cli_text
from yoke.cli.render.base import _sanitize_console_output
from yoke.cli.render.base import _supports_console_chrome


def print_scrollback_divider(console: Console, label: str, *, style: str) -> None:
    """Print a section divider in scrollback output."""
    if console.is_terminal and _supports_console_chrome(console):
        console.print()
        console.rule(f"[{style}]{label}[/{style}]", style=style)
        return
    console.print(f"\n--- {label} ---")


def print_scrollback_separator(console: Console) -> None:
    """Print the standard separator between scrollback sections."""
    if console.is_terminal and _supports_console_chrome(console):
        console.rule(style="dim")
        console.print()
        return
    console.print("---")
    console.print()


def print_scrollback_agent(console: Console, output: str) -> None:
    """Print assistant scrollback content with rich Markdown rendering."""
    output = _sanitize_console_output(console, output.rstrip() or "(empty)")
    console.print()
    if console.is_terminal:
        console.print(Markdown(output))
        console.print()
        return
    console.print(output)
    console.print()


def print_tool_response_divider(console: Console) -> None:
    """Print a subtle divider between tool output and assistant response."""
    if console.is_terminal and _supports_console_chrome(console):
        console.rule(style="dim")
        return
    console.print("---")


def print_scrollback_commentary(console: Console, output: str) -> None:
    """Print assistant commentary with normal Markdown rendering."""
    output = _sanitize_console_output(console, output.rstrip() or "(empty)")
    console.print()
    if console.is_terminal:
        console.print(Markdown(output))
    else:
        console.print(output)
    console.print()


def print_scrollback_tool(console: Console, text: str, *, failed: bool = False) -> None:
    """Print a tool event in scrollback."""
    text = _sanitize_console_output(console, text)
    if console.is_terminal:
        style = "red" if failed else "dim"
        console.print(Text(text, style=style))
        return
    console.print(text)


def print_scrollback_user(console: Console, prompt: str) -> None:
    """Print a user prompt in scrollback."""
    prompt = _sanitize_console_output(console, prompt.rstrip())
    if console.is_terminal and _supports_console_chrome(console):
        console.print(_user_prompt_block(console, prompt))
        return
    console.print(f"user {prompt}", markup=False)


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
    return Text("\n".join(block_lines), style="bold bright_white on #41454c")


def print_scrollback_error(console: Console, message: str) -> None:
    """Print an error block in scrollback."""
    print_scrollback_divider(console, "error", style="red")
    message = _sanitize_console_output(console, message)
    if console.is_terminal:
        console.print(f"[bold red]error[/bold red] {message}")
        return
    console.print(f"error {message}")


def print_scrollback_notice(console: Console, message: str) -> None:
    """Print a note line in scrollback."""
    message = _sanitize_console_output(console, message)
    if console.is_terminal:
        console.print(f"[bold yellow]note[/bold yellow] {message}")
        return
    console.file.write(f"note {message}\n")
    console.file.flush()


def print_session_scrollback(console: Console, messages: list[Message]) -> None:
    """Render session transcript scrollback."""
    emitted_tool_divider = False
    pending_tool_response_divider = False
    for message in _visible_scrollback_messages(messages):
        if message.role == "user":
            print_scrollback_user(console, message.display_text_content() or "")
            emitted_tool_divider = False
            pending_tool_response_divider = False
            continue
        if message.role == "assistant":
            text_content = message.text_content()
            if _starts_tool_activity(message):
                emitted_tool_divider = _ensure_scrollback_tool_divider(
                    console, emitted_tool_divider
                )
                pending_tool_response_divider = True
            if message.commentary_text_content() and text_content:
                print_scrollback_commentary(console, text_content)
            if message.tool_calls:
                if not message.commentary_text_content():
                    console.print()
                for tool_call in message.tool_calls:
                    print_scrollback_tool(
                        console,
                        format_tool_preview(
                            tool_call.function.name,
                            tool_call.function.arguments,
                        ),
                    )
            if text_content and not message.commentary_text_content():
                if pending_tool_response_divider:
                    print_tool_response_divider(console)
                print_scrollback_agent(console, text_content)
                emitted_tool_divider = False
                pending_tool_response_divider = False
            continue
        if message.role == "tool":
            error_text = _tool_result_error(message.plain_text_content)
            if error_text:
                print_scrollback_tool(console, error_text, failed=True)
            pending_tool_response_divider = True


def _ensure_scrollback_tool_divider(
    console: Console, emitted_tool_divider: bool
) -> bool:
    del console
    if emitted_tool_divider:
        return True
    return True


def _starts_tool_activity(message: Message) -> bool:
    return bool(message.tool_calls) or bool(message.commentary_text_content())


def _visible_scrollback_messages(messages: list[Message]) -> list[Message]:
    last_memory_index: int | None = None
    for index, message in enumerate(messages):
        if message.role == "user" and parse_memory_message(
            message.plain_text_content or ""
        ):
            last_memory_index = index
    if last_memory_index is None:
        return messages
    return messages[last_memory_index + 1 :]


def _tool_result_error(content: str | None) -> str | None:
    if not content:
        return None
    try:
        payload = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or payload.get("ok", True):
        return None
    raw_error = payload.get("error")
    if isinstance(raw_error, str) and raw_error.strip():
        return truncate_cli_text(raw_error, 120)
    return "The tool returned an error."
