"""Shared slash-command helpers for command-process inspection."""

from __future__ import annotations

from yoke.agent.tools.command_process_manager import (
    CommandProcessManager,
)
from yoke.cli.render.base import Console


def command_process_manager(agent: object) -> CommandProcessManager | None:
    """Return the live runtime's process manager when supported."""
    manager = getattr(agent, "command_process_manager", None)
    return manager if isinstance(manager, CommandProcessManager) else None


def print_process_table(console: Console, agent: object) -> None:
    """Print the basic-CLI fallback view of live command processes."""
    from rich.table import Table
    from rich.text import Text

    manager = command_process_manager(agent)
    if manager is None:
        from yoke.cli.render import print_scrollback_notice

        print_scrollback_notice(
            console, "Process inspection is unavailable for this agent."
        )
        return
    processes = manager.snapshots()
    if not processes:
        from yoke.cli.render import print_scrollback_notice

        print_scrollback_notice(console, "No command processes yet.")
        return
    table = Table(title="Command Processes", box=None, pad_edge=False)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("PID", justify="right", no_wrap=True)
    table.add_column("Elapsed", justify="right", no_wrap=True)
    table.add_column("Command", overflow="fold")
    styles = {"running": "yellow", "exited": "green", "failed": "red"}
    for process in processes:
        table.add_row(
            str(process.session_id),
            Text(process.status, style=styles[process.status]),
            str(process.pid),
            _format_elapsed(process.elapsed_seconds),
            Text(_safe_text(" ".join(process.command.split()))),
        )
    console.print(table)


def _format_elapsed(seconds: float) -> str:
    if seconds < 10:
        return f"{seconds:.1f}s"
    return f"{seconds:.0f}s"


def _safe_text(text: str) -> str:
    safe: list[str] = []
    for char in text:
        value = ord(char)
        if value <= 0x1F:
            safe.append(chr(0x2400 + value))
        elif value == 0x7F:
            safe.append("␡")
        elif 0x80 <= value <= 0x9F:
            safe.append(f"\\x{value:02x}")
        elif 0xD800 <= value <= 0xDFFF:
            safe.append("�")
        else:
            safe.append(char)
    return "".join(safe)
