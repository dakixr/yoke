"""Command handlers invoked through the interactive command registry."""

from __future__ import annotations

from yoke.agent.models import Message
from yoke.cli.config import CLIArgs
from yoke.cli.image_input import ImageAttachment, resolve_image_path
from yoke.cli.interactive.agents import print_agent_table
from yoke.cli.interactive.command_host import CommandContext
from yoke.cli.interactive.common import COMPACTION_IN_PROGRESS_NOTICE, PendingPrompt
from yoke.cli.interactive.mcp_menu import handle_mcp_menu
from yoke.cli.interactive.model_commands import handle_switch_model
from yoke.cli.interactive.process_commands import print_process_table
from yoke.cli.interactive.queue.commands import handle_queue_command
from yoke.cli.interactive.session_commands import handle_pin_session, print_session_info
from yoke.cli.interactive.skill_commands import handle_skill_load
from yoke.cli.interactive.tools_menu import handle_tools_menu
from yoke.cli.interactive.tree_command import _handle_tree_command
from yoke.cli.render import format_compaction_note, print_scrollback_notice
from yoke.cli.runtime import ActiveSession


def agents(
    ctx: CommandContext, command: str
) -> tuple[bool, list[Message], ActiveSession]:
    active_session = ctx.active_session
    agent = ctx.backend
    console = ctx.console
    messages = ctx.messages
    print_agent_table(console, agent)
    return True, messages, active_session


def ps(ctx: CommandContext, command: str) -> tuple[bool, list[Message], ActiveSession]:
    active_session = ctx.active_session
    agent = ctx.backend
    console = ctx.console
    messages = ctx.messages
    if not ctx.host.inspect_processes():
        print_process_table(console, agent)
    return True, messages, active_session


def image(
    ctx: CommandContext, command: str
) -> tuple[bool, list[Message], ActiveSession]:
    active_session = ctx.active_session
    console = ctx.console
    messages = ctx.messages
    pending_images = ctx.pending_images
    if pending_images is None:
        print_scrollback_notice(console, "Image attachments are not available here.")
        return True, messages, active_session
    raw_path = command.strip()[len("/image ") :].strip()
    try:
        resolved = resolve_image_path(raw_path, root=active_session.root)
    except ValueError as exc:
        print_scrollback_notice(console, str(exc))
        return True, messages, active_session
    attachment = ImageAttachment(path=resolved)
    if not ctx.host.attach_image(attachment):
        pending_images.append(attachment)
        ctx.host.queue_changed()
    print_scrollback_notice(console, f"Attached image: {resolved.name}")
    return True, messages, active_session


def queue(
    ctx: CommandContext, command: str
) -> tuple[bool, list[Message], ActiveSession]:
    active_session = ctx.active_session
    console = ctx.console
    messages = ctx.messages
    pending_prompts = ctx.pending_prompts

    def replace(updated: list[PendingPrompt]) -> str | None:
        handled, notice = ctx.host.replace_queue(updated)
        if not handled and pending_prompts is not None:
            pending_prompts[:] = updated
            ctx.host.queue_changed()
        return notice

    handle_queue_command(
        command.strip().lower(),
        console=console,
        pending_prompts=pending_prompts,
        on_queue_changed=ctx.host.queue_changed,
        on_queue_replace=replace,
    )
    return True, messages, active_session


def skill(
    ctx: CommandContext, command: str
) -> tuple[bool, list[Message], ActiveSession]:
    active_session = ctx.active_session
    agent = ctx.backend
    console = ctx.console
    messages = ctx.messages
    handle_skill_load(
        command,
        agent,
        active_session,
        messages,
        console,
        on_submit_prompt=ctx.host.submit_prompt,
    )
    return True, messages, active_session


def model(
    ctx: CommandContext, command: str
) -> tuple[bool, list[Message], ActiveSession]:
    active_session = ctx.active_session
    agent = ctx.backend
    console = ctx.console
    messages = ctx.messages
    updated_messages = handle_switch_model(
        command,
        agent=agent,
        active_session=active_session,
        messages=messages,
        console=console,
        args=CLIArgs(
            model=(
                f"{active_session.record.provider_name}:"
                f"{active_session.record.model_id}"
                if active_session.record.provider_name
                and active_session.record.model_id
                else active_session.record.model_id
            ),
            reasoning_effort=active_session.record.reasoning_effort,
            root=str(active_session.root),
        ),
    )
    return True, updated_messages, active_session


def tools(
    ctx: CommandContext, command: str
) -> tuple[bool, list[Message], ActiveSession]:
    active_session = ctx.active_session
    agent = ctx.backend
    console = ctx.console
    messages = ctx.messages
    handle_tools_menu(
        agent=agent,
        console=console,
        root=active_session.root,
    )
    return True, messages, active_session


def mcp(ctx: CommandContext, command: str) -> tuple[bool, list[Message], ActiveSession]:
    active_session = ctx.active_session
    agent = ctx.backend
    console = ctx.console
    messages = ctx.messages
    raw_server = command.strip()[len("/mcp") :].strip() or None
    handle_mcp_menu(
        agent=agent,
        console=console,
        root=active_session.root,
        initial_server=raw_server,
    )
    return True, messages, active_session


def title(
    ctx: CommandContext, command: str
) -> tuple[bool, list[Message], ActiveSession]:
    active_session = ctx.active_session
    agent = ctx.backend
    console = ctx.console
    messages = ctx.messages
    raw_title = command.strip()[len("/title") :].strip()
    if not raw_title:
        return True, messages, active_session
    agent.title(raw_title, messages)
    print_scrollback_notice(
        console,
        f"Updated session title: {active_session.title}",
    )
    return True, messages, active_session


def regenerate_title(
    ctx: CommandContext, command: str
) -> tuple[bool, list[Message], ActiveSession]:
    active_session = ctx.active_session
    agent = ctx.backend
    console = ctx.console
    messages = ctx.messages
    generated_title = agent.regenerate_title(messages)
    if generated_title is None:
        print_scrollback_notice(console, "Could not regenerate session title.")
        return True, messages, active_session
    print_scrollback_notice(
        console,
        f"Updated session title: {active_session.title}",
    )
    return True, messages, active_session


def pin(ctx: CommandContext, command: str) -> tuple[bool, list[Message], ActiveSession]:
    active_session = ctx.active_session
    agent = ctx.backend
    console = ctx.console
    messages = ctx.messages
    print_scrollback_notice(
        console,
        handle_pin_session(active_session, agent, messages),
    )
    return True, messages, active_session


def info(
    ctx: CommandContext, command: str
) -> tuple[bool, list[Message], ActiveSession]:
    active_session = ctx.active_session
    console = ctx.console
    messages = ctx.messages
    print_session_info(console, active_session, backend=ctx.backend)
    return True, messages, active_session


def fork(
    ctx: CommandContext, command: str
) -> tuple[bool, list[Message], ActiveSession]:
    active_session = ctx.active_session
    agent = ctx.backend
    console = ctx.console
    messages = ctx.messages
    forked_session = agent.fork(messages)
    print_scrollback_notice(
        console,
        f"Forked session {active_session.id} -> {forked_session.id}",
    )
    return True, forked_session.messages(), forked_session


def tree(
    ctx: CommandContext, command: str
) -> tuple[bool, list[Message], ActiveSession]:
    active_session = ctx.active_session
    agent = ctx.backend
    console = ctx.console
    messages = ctx.messages
    if not ctx.host.supports_tree:
        print_scrollback_notice(
            console, "/tree is only available in the prompt-toolkit TUI."
        )
        return True, messages, active_session
    result = _handle_tree_command(
        active_session,
        agent,
        console,
        host=ctx.host,
    )
    if result is None:
        return True, messages, active_session
    return True, result[0], result[1]


def compact(
    ctx: CommandContext, command: str
) -> tuple[bool, list[Message], ActiveSession]:
    active_session = ctx.active_session
    agent = ctx.backend
    console = ctx.console
    messages = ctx.messages
    if ctx.host.start_compaction():
        return True, messages, active_session
    print_scrollback_notice(console, COMPACTION_IN_PROGRESS_NOTICE)
    compacted = agent.compact(
        messages,
        conversation_entries=active_session.active_entries(),
    )
    if compacted is None:
        print_scrollback_notice(console, "Nothing to compact right now.")
        return True, messages, active_session
    (
        updated_messages,
        _preparation,
        _result,
        conversation_entries,
        compaction_payload,
        usage_payload,
    ) = compacted
    agent.persist(
        updated_messages,
        conversation_entries=conversation_entries,
    )
    print_scrollback_notice(
        console,
        format_compaction_note(compaction_payload),
    )
    ctx.host.update_context_usage(usage_payload)
    return True, updated_messages, active_session


def new(ctx: CommandContext, command: str) -> tuple[bool, list[Message], ActiveSession]:
    agent = ctx.backend
    console = ctx.console
    new_session = agent.new()
    print_scrollback_notice(
        console,
        f"Started new session {new_session.id}",
    )
    ctx.host.update_context_usage({"usage_percent": 0})
    return True, [], new_session


def shortcuts(
    ctx: CommandContext, command: str
) -> tuple[bool, list[Message], ActiveSession]:
    from yoke.cli.interactive.prompt.keys import shortcut_notice

    print_scrollback_notice(ctx.console, shortcut_notice(basic=ctx.host.basic_mode))
    return True, ctx.messages, ctx.active_session
