"""Prompt-toolkit interactive CLI loop."""

from __future__ import annotations

import sys
from collections.abc import Callable
from threading import Lock
from typing import TYPE_CHECKING
from typing import cast

from yoke.agent.models import Message
from yoke.agent.state import active_branch_entries
from yoke.cli.config.args import CLIArgs
from yoke.cli.config.runtime import format_provider_model_status
from yoke.cli.image_input import ImageAttachment
from yoke.cli.image_input import resolve_image_path
from yoke.cli.interactive.completion import SlashCommandCompleter
from yoke.cli.interactive.common import PromptCliState
from yoke.cli.interactive.common import estimate_context_usage_text
from yoke.cli.interactive.common import format_context_usage_text
from yoke.cli.path_display import format_root_label
from yoke.cli.interactive.prompt.keys import (
    cycle_prompt_thinking_effort,
    register_prompt_toolkit_key_bindings,
)
from yoke.cli.interactive.prompt.control import (
    create_prompt_toolkit_control,
)
from yoke.cli.interactive.prompt.loop import (
    run_prompt_toolkit_event_loop,
)
from yoke.cli.interactive.prompt.paste import (
    patch_prompt_toolkit_input_for_multiline_paste,
)
from yoke.cli.interactive.prompt.rendering import (
    initialize_prompt_toolkit_session,
)
from yoke.cli.interactive.prompt.rendering import run_scrollback_render
from yoke.cli.interactive.queue.persistence import load_prompt_queue
from yoke.cli.interactive.queue.persistence import persist_prompt_queue
from yoke.cli.interactive.renderer import PromptToolkitLiveRenderer
from yoke.cli.interactive.tools.inspector import open_live_tool_inspector
from yoke.cli.interactive.tools.trace import ToolTraceStore
from yoke.cli.interactive.tools.trace import entries_from_messages
from yoke.cli.interactive.tools.trace import merge_trace_entries
from yoke.cli.render import OutputStream
from yoke.cli.render import build_console
from yoke.cli.render import print_scrollback_agent
from yoke.cli.render import print_scrollback_commentary
from yoke.cli.render import print_scrollback_error
from yoke.cli.render import print_scrollback_notice
from yoke.cli.render import print_scrollback_tool
from yoke.cli.render import print_tool_response_divider
from yoke.cli.runtime import ActiveSession
from yoke.cli.runtime import AgentRunner
from yoke.cli.runtime.terminal_output_gate import defer_until_fullscreen_exits

if TYPE_CHECKING:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.input.base import Input
    from prompt_toolkit.output.base import Output
    from yoke.ai.providers.base import ProviderModelInfo


def _format_duration(seconds: float) -> str:
    """Format a duration for the 'Worked for ...' summary line."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = int(seconds // 60)
    remaining = int(seconds % 60)
    if minutes < 60:
        return f"{minutes}m{remaining:02d}s"
    hours = minutes // 60
    remaining_minutes = minutes % 60
    return f"{hours}h{remaining_minutes:02d}m"


def run_prompt_toolkit_cli(  # noqa: C901
    args: CLIArgs,
    agent: AgentRunner,
    session_messages: list[Message],
    *,
    active_session: ActiveSession,
    pt_input: Input | None = None,
    pt_output: Output | None = None,
    on_app_created: Callable[[object], None] | None = None,
    replay_session: bool = False,
) -> int:
    """Run the prompt-toolkit interactive CLI."""
    from prompt_toolkit import PromptSession
    from prompt_toolkit.application.run_in_terminal import run_in_terminal
    from prompt_toolkit.key_binding import KeyBindings

    restored_prompts, restored_images = load_prompt_queue(active_session)
    state = PromptCliState(
        messages=list(session_messages),
        pending_prompts=restored_prompts,
        pending_images=restored_images,
        abandoned_turn_ids=set(),
        steered_turn_ids=set(),
        thinking_effort=args.reasoning_effort,
    )
    provider_model_text = format_provider_model_status(agent)

    def refresh_provider_model_text() -> str | None:
        return format_provider_model_status(agent)

    def refresh_session_title_text() -> str | None:
        return session_ref["active_session"].title

    spinner_frames = ("▰▱▱", "▱▰▱", "▱▱▰", "▱▰▱")
    state_lock = Lock()
    prompt_session: PromptSession[str] = PromptSession(
        input=pt_input,
        output=pt_output,
        erase_when_done=True,
    )
    patch_prompt_toolkit_input_for_multiline_paste(prompt_session)
    if on_app_created is not None:
        on_app_created(prompt_session)
    scrollback_console = build_console(cast(OutputStream, sys.stdout))
    session_ref: dict[str, ActiveSession] = {"active_session": active_session}
    root_label = format_root_label(active_session.root)
    tool_trace_store = ToolTraceStore()
    if restored_prompts or restored_images:
        print_scrollback_notice(
            scrollback_console,
            f"Restored {len(restored_prompts)} queued prompt(s). Use /queue or Ctrl+Q to review.",
        )

    def estimate_toolbar_context(prompt: str = "") -> str | None:
        with state_lock:
            message_snapshot = list(state.messages)
            current_session = session_ref["active_session"]
            conversation_entries_snapshot = active_branch_entries(
                current_session.record.conversation_entries,
                leaf_id=current_session.record.leaf_id,
            )
        return estimate_context_usage_text(
            agent,
            prompt,
            message_snapshot,
            conversation_entries=conversation_entries_snapshot,
        )

    def invalidate_prompt() -> None:
        app = prompt_session.app
        loop = app.loop
        if loop is not None:
            loop.call_soon_threadsafe(app.invalidate)

    def update_status(message: str) -> None:
        with state_lock:
            state.status_message = message
        invalidate_prompt()

    def update_context_usage(usage_text: str | None) -> None:
        with state_lock:
            state.context_usage_text = usage_text
        invalidate_prompt()

    def update_context_details(details: dict[str, int | None]) -> None:
        with state_lock:
            state.context_usage_percent = details.get("usage_percent")
            state.context_input_tokens = details.get("input_tokens")
            state.context_max_tokens = details.get("max_tokens")
        invalidate_prompt()

    def update_turn_tokens(tokens: dict[str, int | None]) -> None:
        with state_lock:
            state.turn_input_tokens = tokens.get("input_tokens")
            state.turn_output_tokens = tokens.get("output_tokens")
            state.turn_reasoning_tokens = tokens.get("reasoning_tokens")
            if state.turn_input_tokens is not None:
                state.session_input_tokens += state.turn_input_tokens
            if state.turn_output_tokens is not None:
                state.session_output_tokens += state.turn_output_tokens
        invalidate_prompt()

    def increment_tool_count() -> None:
        with state_lock:
            state.turn_tool_count += 1
            state.session_tool_calls += 1
        invalidate_prompt()

    def emit_turn_summary(summary: dict[str, object]) -> None:
        duration = summary.get("duration_seconds")
        if not isinstance(duration, (int, float)):
            return
        text = f"Worked for {_format_duration(duration)}"
        tools = summary.get("tool_count")
        if isinstance(tools, int) and tools > 0:
            text += f" \u00b7 {tools} tool{'s' if tools != 1 else ''}"

        def _print() -> None:
            from rich.text import Text

            scrollback_console.print(Text(text, style="dim"))

        run_in_scrollback(_print)

    def run_in_scrollback(render: Callable[[], None]) -> None:
        if defer_until_fullscreen_exits(lambda: run_in_scrollback(render)):
            return
        app = prompt_session.app
        loop = app.loop
        if loop is None:
            render()
            return
        loop.call_soon_threadsafe(
            lambda: run_scrollback_render(
                loop=loop,
                render=render,
                run_in_terminal=run_in_terminal,
            )
        )

    state.context_usage_text = estimate_toolbar_context()
    renderer = PromptToolkitLiveRenderer(
        begin_tool_block=lambda: None,
        emit_tool_response_divider=lambda: run_in_scrollback(
            lambda: print_tool_response_divider(scrollback_console)
        ),
        emit_tool=lambda text, failed: run_in_scrollback(
            lambda: print_scrollback_tool(
                scrollback_console,
                text,
                failed=failed,
            )
        ),
        emit_agent=lambda text: run_in_scrollback(
            lambda: print_scrollback_agent(scrollback_console, text)
        ),
        emit_commentary=lambda text: run_in_scrollback(
            lambda: print_scrollback_commentary(scrollback_console, text)
        ),
        emit_error=lambda text: run_in_scrollback(
            lambda: print_scrollback_error(scrollback_console, text)
        ),
        emit_notice=lambda text: run_in_scrollback(
            lambda: print_scrollback_notice(scrollback_console, text)
        ),
        set_status=update_status,
        set_context_usage=update_context_usage,
        set_context_details=update_context_details,
        set_turn_tokens=update_turn_tokens,
        increment_tool_count=increment_tool_count,
        emit_turn_summary=emit_turn_summary,
        record_tool_event=tool_trace_store.record_event,
    )
    key_bindings = KeyBindings()

    def show_tool_inspector() -> None:
        def current_entries():
            with state_lock:
                message_snapshot = list(state.messages)
            return merge_trace_entries(
                entries_from_messages(message_snapshot),
                tool_trace_store.snapshot(),
            )

        app = prompt_session.app
        loop = app.loop
        if loop is None:
            open_live_tool_inspector(
                current_entries,
                trace_store=tool_trace_store,
            )
            return
        loop.call_soon_threadsafe(
            lambda: run_in_terminal(
                lambda: open_live_tool_inspector(
                    current_entries,
                    trace_store=tool_trace_store,
                ),
                in_executor=True,
            )
        )

    def open_model_selector(preserved_text: str) -> None:
        with state_lock:
            state.next_editor_text = preserved_text

    def open_tree_selector(preserved_text: str) -> None:
        with state_lock:
            state.next_editor_text = preserved_text

    def open_queue_selector(preserved_text: str) -> None:
        with state_lock:
            state.next_editor_text = preserved_text

    def attach_image(attachment: ImageAttachment) -> None:
        with state_lock:
            state.pending_images.append(attachment)
            persist_prompt_queue(
                session_ref["active_session"],
                state.pending_prompts,
                state.pending_images,
            )
        update_status(f"Attached image: {attachment.label}")

    def remove_pending_image(index: int = -1) -> None:
        with state_lock:
            if not state.pending_images:
                return
            if index < 0:
                index = len(state.pending_images) - 1
            if index >= len(state.pending_images):
                return
            removed = state.pending_images.pop(index)
            persist_prompt_queue(
                session_ref["active_session"],
                state.pending_prompts,
                state.pending_images,
            )
        update_status(
            "Removed image attachment: "
            f"{removed.label}. Edit its prompt reference if needed."
        )
        invalidate_prompt()

    control = create_prompt_toolkit_control(
        state=state,
        agent=agent,
        active_session_ref=session_ref,
        renderer=renderer,
        scrollback_console=scrollback_console,
        state_lock=state_lock,
        estimate_toolbar_context_usage=estimate_toolbar_context,
        invalidate_prompt=invalidate_prompt,
        update_status=update_status,
        run_in_scrollback=run_in_scrollback,
    )

    def cycle_thinking_effort() -> str | None:
        provider = getattr(agent, "provider", None)
        config = getattr(provider, "config", None)
        current = getattr(config, "reasoning_effort", None)
        current_model_info = getattr(provider, "current_model_info", None)
        model_info = (
            cast("ProviderModelInfo | None", current_model_info())
            if callable(current_model_info)
            else None
        )
        next_effort = cycle_prompt_thinking_effort(
            current,
            available_efforts=(
                model_info.thinking_levels if model_info is not None else None
            ),
        )
        if config is not None and hasattr(config, "reasoning_effort"):
            config.reasoning_effort = next_effort
        nonlocal provider_model_text
        provider_model_text = refresh_provider_model_text()
        invalidate_prompt()
        return next_effort

    register_prompt_toolkit_key_bindings(
        key_bindings,
        state=state,
        stop_active_turn=control.stop_active_turn,
        attach_image=attach_image,
        remove_last_image=lambda: remove_pending_image(),
        resolve_image_path=lambda raw: resolve_image_path(
            raw, root=session_ref["active_session"].root
        ),
        cycle_thinking_effort=cycle_thinking_effort,
        open_tool_inspector=show_tool_inspector,
        open_model_selector=open_model_selector,
        open_tree_selector=open_tree_selector,
        open_queue_manager=open_queue_selector,
        update_status=update_status,
    )
    initialize_prompt_toolkit_session(
        state=state,
        replay_session=replay_session,
        scrollback_console=scrollback_console,
        start_turn=control.start_turn,
    )
    return run_prompt_toolkit_event_loop(
        state=state,
        active_session_ref=session_ref,
        agent=agent,
        prompt_session=prompt_session,
        completer=SlashCommandCompleter(
            skill_provider=lambda: getattr(agent, "available_skills", ()),
        ),
        key_bindings=key_bindings,
        state_lock=state_lock,
        scrollback_console=scrollback_console,
        provider_model_text=refresh_provider_model_text,
        session_title_text=refresh_session_title_text,
        spinner_frames=spinner_frames,
        root_label=root_label,
        request_exit=control.request_exit,
        update_status=update_status,
        invalidate_prompt=invalidate_prompt,
        start_turn=control.start_turn,
        start_compaction=control.start_compaction,
        steer_active_turn=control.steer_active_turn,
        format_context_usage_text=format_context_usage_text,
        estimate_toolbar_context_usage=estimate_toolbar_context,
    )
