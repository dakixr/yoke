"""Canonical interactive commands, dispatch, completion and lifecycle policy."""

from __future__ import annotations

from yoke.session.workspace import require_workspace

from collections.abc import Callable
from dataclasses import dataclass

from yoke.agent.models import Message
from yoke.cli.image_input import ImageAttachment
from yoke.cli.interactive import slash_commands as handlers
from yoke.cli.interactive.command_host import (
    CommandCallbacks,
    CommandContext,
    CommandHost,
)
from yoke.cli.interactive.common import PendingPrompt
from yoke.cli.render import print_scrollback_notice
from yoke.cli.render.base import Console
from yoke.cli.backend.daemon import DaemonActiveSession
from yoke.cli.backend.daemon import DaemonSessionBackend
from yoke.session.workspace import WorkspaceUnavailable


@dataclass(frozen=True, slots=True)
class Command:
    """A command's user interface and execution policy."""

    name: str
    description: str
    handler: Callable[
        [CommandContext, str], tuple[bool, list[Message], DaemonActiveSession]
    ]
    aliases: tuple[str, ...] = ()
    usage: str | None = None
    takes_args: bool = False
    # Arguments to an argument-less command normally make the text a model
    # prompt ("/help me fix this", "? why"). These report a usage error instead.
    rejects_args: bool = False
    needs_terminal: bool = True
    available_in_basic: bool = True
    min_args: int = 0
    changes_session: bool = False

    @property
    def usage_text(self) -> str:
        return self.name + (f" {self.usage}" if self.usage else "")


def show_help(
    ctx: CommandContext, _command: str
) -> tuple[bool, list[Message], DaemonActiveSession]:
    lines = [
        f"{item.usage_text}{' (' + ', '.join(item.aliases) + ')' if item.aliases else ''}  {item.description}"
        for item in SLASH_COMMANDS
        if not ctx.host.basic_mode or item.available_in_basic
    ]
    print_scrollback_notice(ctx.console, "Commands:\n" + "\n".join(lines))
    return True, ctx.messages, ctx.active_session


SLASH_COMMANDS: tuple[Command, ...] = (
    Command(
        "/compact",
        "Summarize older conversation context into memory.",
        handlers.compact,
        needs_terminal=False,
    ),
    Command(
        "/shortcuts",
        "Show interactive keyboard shortcuts.",
        handlers.shortcuts,
        aliases=("?",),
    ),
    Command(
        "/new",
        "Start a fresh session in the current workspace.",
        handlers.new,
        changes_session=True,
    ),
    Command(
        "/pin",
        "Pin or unpin the active session.",
        handlers.pin,
        rejects_args=True,
    ),
    Command(
        "/info",
        "Show active session metadata.",
        handlers.info,
        rejects_args=True,
    ),
    Command(
        "/fork",
        "Copy this session and continue in the fork.",
        handlers.fork,
        changes_session=True,
        rejects_args=True,
    ),
    Command(
        "/title",
        "Rename the active session.",
        handlers.title,
        usage="<new-title>",
        min_args=1,
        takes_args=True,
    ),
    Command(
        "/regenerate-title",
        "Generate a new title from this conversation.",
        handlers.regenerate_title,
        rejects_args=True,
    ),
    Command(
        "/tree",
        "Navigate the current session tree.",
        handlers.tree,
        available_in_basic=False,
    ),
    Command(
        "/model",
        "Open the model switcher.",
        handlers.model,
        takes_args=True,
    ),
    Command(
        "/tools",
        "Toggle available tools for this run only.",
        handlers.tools,
    ),
    Command(
        "/mcp",
        "Manage MCP servers and tools for this session, repo, or globally.",
        handlers.mcp,
        usage="server",
        takes_args=True,
    ),
    Command(
        "/queue",
        "Open the interactive prompt queue manager.",
        handlers.queue,
        available_in_basic=False,
        rejects_args=True,
    ),
    Command(
        "/ps",
        "Inspect command processes for this live runtime.",
        handlers.ps,
        needs_terminal=False,
        rejects_args=True,
    ),
    Command(
        "/agents",
        "Show the latest SDK agent runs for this live runtime.",
        handlers.agents,
        rejects_args=True,
    ),
    Command(
        "/image",
        "Attach an image file to the next prompt.",
        handlers.image,
        usage="<path>",
        min_args=1,
        takes_args=True,
    ),
    Command(
        "/skill",
        "Activate a discovered skill for this session.",
        handlers.skill,
        usage="<name> [prompt]",
        min_args=1,
        takes_args=True,
    ),
    Command("/help", "Show available commands.", show_help),
)
COMMANDS_BY_NAME = {
    name: item for item in SLASH_COMMANDS for name in (item.name, *item.aliases)
}


def find_command(text: str) -> Command | None:
    """Return the command this text invokes, or None for a model prompt."""
    words = text.strip().split(maxsplit=1)
    if not words:
        return None
    item = COMMANDS_BY_NAME.get(words[0].lower())
    if item is None or len(words) == 1 or item.takes_args or item.rejects_args:
        return item
    return None


def handle_slash_command(
    command: str,
    *,
    agent: DaemonSessionBackend,
    active_session: DaemonActiveSession,
    messages: list[Message],
    console: Console,
    pending_images: list[ImageAttachment] | None = None,
    pending_prompts: list[PendingPrompt] | None = None,
    host: CommandHost | None = None,
) -> tuple[bool, list[Message], DaemonActiveSession]:
    """Validate and dispatch one registered command."""
    item = find_command(command)
    if item is None:
        # Unrecognized slash text remains an ordinary model prompt.
        return False, messages, active_session
    host = host or CommandCallbacks()
    ctx = CommandContext(
        agent,
        active_session,
        messages,
        console,
        host,
        pending_images,
        pending_prompts,
    )
    if host.basic_mode and not item.available_in_basic:
        print_scrollback_notice(
            console, f"{item.name} is only available in the prompt-toolkit TUI."
        )
        return True, messages, active_session
    if not item.takes_args and len(command.strip().split(maxsplit=1)) > 1:
        print_scrollback_notice(console, f"Usage: {item.usage_text}")
        return True, messages, active_session
    if len(command.strip().split()) - 1 < item.min_args:
        print_scrollback_notice(console, f"Usage: {item.usage_text}")
        return True, messages, active_session
    if item.changes_session:
        try:
            require_workspace(active_session.root, session_id=active_session.id)
        except WorkspaceUnavailable as exc:
            print_scrollback_notice(console, str(exc))
            return True, messages, active_session
    return item.handler(ctx, command)
