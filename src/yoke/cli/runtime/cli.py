"""CLI entrypoints for yoke runtime."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from rich.text import Text

from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import ConversationEntry
from yoke.agent.models import Message
from yoke.agent.state import active_branch_entries
from yoke.agent.state import capture_agent_state
from yoke.cli.bootstrap.types import ToolLoadReport
from yoke.cli.config import CLIArgs
from yoke.cli.config import RUN_ERRORS
from yoke.cli.config import build_cli_agent_from_args
from yoke.cli.image_input import build_user_message
from yoke.cli.render import OutputStream
from yoke.cli.render import build_console
from yoke.cli.render import print_agent_output
from yoke.cli.render import print_error
from yoke.cli.runtime.base import ActiveSession
from yoke.cli.runtime.base import AgentRunner
from yoke.cli.runtime.base import ToolReportAgent
from yoke.cli.runtime.base import execute_turn
from yoke.cli.runtime.session import create_active_session
from yoke.cli.runtime.session import apply_session_defaults_to_args
from yoke.cli.runtime.session import ensure_session_title
from yoke.cli.runtime.session import persist_session_state
from yoke.cli.runtime.session import save_active_session
from yoke.cli.runtime.session import save_agent_session_state
from yoke.cli.runtime.session import select_session_id
from yoke.cli.session import SessionStore


@dataclass(slots=True)
class CLIMode:
    """Resolved CLI execution mode."""

    kind: str
    prompt: str | None = None
    images: tuple[str, ...] = ()


def print_tool_discovery_message(
    stream: OutputStream, report: ToolLoadReport | None
) -> None:
    """Print the formatted tool discovery summary."""
    if report is None:
        return
    from yoke.cli.config import format_tool_discovery_message

    build_console(stream).print(
        Text(format_tool_discovery_message(report), style="dim")
    )


def resolve_cli_mode(
    args: CLIArgs,
    *,
    input_func: Callable[..., str],
) -> CLIMode:
    """Resolve interactive versus headless CLI mode."""
    if args.headless:
        if args.prompt:
            return CLIMode(
                kind="headless",
                prompt=args.prompt,
                images=args.images,
            )
        if sys.stdin.isatty():
            raise ValueError(
                "Headless mode requires --prompt or prompt text from stdin."
            )
        prompt = input_func().strip()
        if not prompt:
            raise ValueError("Headless mode requires non-empty prompt text from stdin.")
        return CLIMode(kind="headless", prompt=prompt, images=args.images)
    if args.images and not args.prompt:
        raise ValueError("Interactive startup images require --prompt as well.")
    return CLIMode(kind="interactive", prompt=args.prompt, images=args.images)


def run_cli(
    args: CLIArgs,
    agent: AgentRunner | None = None,
    *,
    input_func=input,
    stdout: OutputStream | None = None,
    stderr: OutputStream | None = None,
) -> int:
    """Run the yoke CLI for a fresh session."""
    error_stream = stderr or sys.stderr
    output_stream = stdout or sys.stdout
    error_console = build_console(error_stream)
    tool_report: ToolLoadReport | None = None
    try:
        active_agent, tool_report = _resolve_runtime_agent(args, agent=agent)
        mode = resolve_cli_mode(args, input_func=input_func)
    except ValueError as exc:
        print_error(error_console, str(exc))
        return 1
    active_session = create_active_session(args, root=Path(args.root))
    if isinstance(active_agent, RuntimeAgent):
        active_agent.load_conversation(
            conversation_entries=active_branch_entries(
                active_session.record.conversation_entries,
                leaf_id=active_session.record.leaf_id,
            ),
            active_skills=active_session.record.active_skills,
        )
    save_active_session(
        active_session,
        active_session.record.messages,
        conversation_entries=active_session.record.conversation_entries,
        leaf_id=active_session.record.leaf_id,
        agent=active_agent,
    )
    session_messages = active_session.record.messages
    if mode.kind == "headless":
        try:
            return _run_headless_mode(
                args=args,
                active_agent=active_agent,
                active_session=active_session,
                session_messages=session_messages,
                prompt=mode.prompt,
                image_paths=mode.images,
                error_stream=error_stream,
                error_console=error_console,
                output_console=build_console(output_stream),
            )
        except ValueError as exc:
            print_error(error_console, str(exc))
            return 1
    if mode.prompt is not None:
        try:
            resolved_images = _resolve_image_paths(
                mode.images, root=active_session.root
            )
        except ValueError as exc:
            print_error(error_console, str(exc))
            return 1
        session_messages.append(
            build_user_message(mode.prompt, image_paths=resolved_images)
        )
        save_active_session(active_session, session_messages)
        ensure_session_title(
            active_session, active_agent, mode.prompt, stderr=error_stream
        )
    print_tool_discovery_message(output_stream, tool_report)
    from yoke.cli.interactive import run_interactive_cli

    return run_interactive_cli(
        args,
        active_agent,
        session_messages,
        active_session=active_session,
        input_func=input_func,
        stdout=output_stream,
        stderr=error_stream,
    )


def run_resume_cli(
    args: CLIArgs,
    session_id: str | None,
    *,
    all_sessions: bool = False,
    agent: AgentRunner | None = None,
    input_func=input,
    stdout: OutputStream | None = None,
    stderr: OutputStream | None = None,
) -> int:
    """Resume a saved yoke session."""
    output_stream = stdout or sys.stdout
    error_stream = stderr or sys.stderr
    output_console = build_console(output_stream)
    error_console = build_console(error_stream)
    tool_report: ToolLoadReport | None = None
    store = SessionStore()
    root = Path(args.root).resolve()
    if session_id is None:
        try:
            session_id = select_session_id(
                store,
                root=root,
                all_sessions=all_sessions,
                input_func=input_func,
                stdout=output_stream,
            )
        except ValueError as exc:
            print_error(error_console, str(exc))
            return 1
        output_console.print(f"Resuming session {session_id}")
    try:
        record = store.load(session_id)
    except ValueError as exc:
        print_error(error_console, str(exc))
        return 1
    if record.created_at is None and not record.messages:
        print_error(error_console, f"Session not found: {session_id}")
        return 1
    session_root = Path(record.root).resolve() if record.root else root
    args.root = str(session_root)
    apply_session_defaults_to_args(args, record)
    try:
        active_agent, tool_report = _resolve_runtime_agent(args, agent=agent)
    except ValueError as exc:
        print_error(error_console, str(exc))
        return 1
    active_session = ActiveSession(
        id=session_id,
        root=session_root,
        store=store,
        record=record,
        title=record.title,
    )
    if record.active_skills and isinstance(active_agent, RuntimeAgent):
        active_agent.active_skills = list(record.active_skills)
    if isinstance(active_agent, RuntimeAgent):
        active_agent.load_conversation(
            conversation_entries=active_branch_entries(
                record.conversation_entries,
                leaf_id=record.leaf_id,
            ),
            active_skills=record.active_skills,
        )
    save_active_session(
        active_session,
        active_session.record.messages,
        conversation_entries=active_session.record.conversation_entries,
        leaf_id=active_session.record.leaf_id,
        agent=active_agent,
    )
    print_tool_discovery_message(output_stream, tool_report)
    from yoke.cli.interactive import run_interactive_cli

    return run_interactive_cli(
        args,
        active_agent,
        record.messages,
        active_session=active_session,
        input_func=input_func,
        stdout=output_stream,
        stderr=error_stream,
        replay_session=True,
    )


def _resolve_runtime_agent(
    args: CLIArgs,
    *,
    agent: AgentRunner | None,
) -> tuple[AgentRunner, ToolLoadReport | None]:
    if agent is None:
        built_agent = build_cli_agent_from_args(args)
        return built_agent.agent, built_agent.tool_report
    tool_report = agent.tool_report if isinstance(agent, ToolReportAgent) else None
    return agent, tool_report


def _run_headless_mode(
    *,
    args: CLIArgs,
    active_agent: AgentRunner,
    active_session: ActiveSession,
    session_messages: list[Message],
    prompt: str | None,
    image_paths: tuple[str, ...],
    error_stream: OutputStream,
    error_console,
    output_console,
) -> int:
    del args
    if prompt is None:
        raise ValueError("Headless mode requires a prompt.")
    previous_yoke_headless = os.environ.get("YOKE_HEADLESS")
    os.environ["YOKE_HEADLESS"] = "1"
    try:
        ensure_session_title(
            active_session,
            active_agent,
            prompt,
            stderr=error_stream,
        )
        result = execute_turn(
            active_agent,
            prompt,
            session_messages,
            stderr=error_stream,
            user_message=build_user_message(
                prompt,
                image_paths=_resolve_image_paths(image_paths, root=active_session.root),
            ),
            conversation_entries=active_branch_entries(
                active_session.record.conversation_entries,
                leaf_id=active_session.record.leaf_id,
            ),
            active_skills=active_session.record.active_skills,
            available_skills=(
                active_agent.available_skills
                if isinstance(active_agent, RuntimeAgent)
                else []
            ),
        )
    except RUN_ERRORS as exc:
        partial_state = capture_agent_state(
            active_agent,
            messages=_partial_messages_from_error(exc),
            conversation_entries=_partial_entries_from_error(exc),
        )
        if partial_state.messages or partial_state.conversation_entries:
            save_agent_session_state(
                active_session,
                partial_state,
                agent=active_agent,
            )
        print_error(error_console, str(exc))
        return 1
    finally:
        if previous_yoke_headless is None:
            os.environ.pop("YOKE_HEADLESS", None)
        else:
            os.environ["YOKE_HEADLESS"] = previous_yoke_headless
    persist_session_state(
        active_session,
        active_agent,
        result.messages,
        conversation_entries=result.conversation_entries,
    )
    print_agent_output(output_console, result.output)
    return 0


def _resolve_image_paths(
    image_values: tuple[str, ...], *, root: Path
) -> tuple[Path, ...]:
    from yoke.cli.image_input import resolve_image_path

    return tuple(resolve_image_path(value, root=root) for value in image_values)


def _partial_messages_from_error(exc: BaseException) -> list[Message] | None:
    partial_messages = getattr(exc, "partial_messages", None)
    if isinstance(partial_messages, list) and all(
        isinstance(message, Message) for message in partial_messages
    ):
        return partial_messages
    return None


def _partial_entries_from_error(
    exc: BaseException,
) -> list[ConversationEntry] | None:
    partial_entries = getattr(exc, "partial_conversation_entries", None)
    if isinstance(partial_entries, list) and all(
        isinstance(entry, ConversationEntry) for entry in partial_entries
    ):
        return partial_entries
    return None
