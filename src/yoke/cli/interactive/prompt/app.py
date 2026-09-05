"""Prompt-toolkit interactive CLI loop."""

from __future__ import annotations

import sys
from collections.abc import Callable
from threading import Lock
from typing import TYPE_CHECKING
from typing import cast

from yoke.agent.models import Message
from yoke.cli.config import CLIArgs
from yoke.cli.config import format_provider_model_status
from yoke.cli.image_input import ImageAttachment
from yoke.cli.image_input import resolve_image_path
from yoke.cli.interactive.completion import SlashCommandCompleter
from yoke.cli.interactive.common import PromptCliState
from yoke.cli.interactive.common import estimate_context_usage_text
from yoke.cli.interactive.common import format_context_usage_text
from yoke.cli.path_display import format_root_label
from yoke.cli.interactive.prompt.keys import (
    cycle_prompt_thinking_effort,
    insert_attachment_reference,
    register_prompt_toolkit_key_bindings,
)
from yoke.cli.interactive.prompt.clipboard import ClipboardPasteResult
from yoke.cli.interactive.prompt.clipboard import start_clipboard_paste
from yoke.cli.interactive.prompt.control import (
    create_prompt_toolkit_control,
)  # noqa: E501
from yoke.cli.interactive.prompt.inspectors import (
    create_inspector_launchers,
)
from yoke.cli.interactive.prompt.context_usage import ContextUsageWorker
from yoke.cli.interactive.prompt.loop import (
    run_prompt_toolkit_event_loop,
)  # noqa: E501
from yoke.cli.interactive.prompt.summary import format_turn_summary
from yoke.cli.interactive.prompt.win32 import (
    patch_prompt_toolkit_win32_executor_shutdown,
)
from yoke.cli.interactive.prompt.paste import (
    patch_prompt_toolkit_input_for_multiline_paste,
)
from yoke.cli.interactive.queue.mutations import attach_pending_image
from yoke.cli.interactive.queue.mutations import (
    remove_pending_image as persist_remove_pending_image,
)
from yoke.cli.interactive.queue.persistence import load_prompt_queue_state
from yoke.cli.interactive.prompt.rendering import (
    initialize_prompt_toolkit_session,
)  # noqa: E501
from yoke.cli.interactive.renderer import PromptToolkitLiveRenderer
from yoke.cli.interactive.tool_inspector import ToolTraceStore
from yoke.cli.render import OutputStream
from yoke.cli.render import build_console
from yoke.cli.runtime import ActiveSession
from yoke.cli.runtime import AgentRunner
from yoke.cli.interactive.prompt.scrollback import BatchedScrollback

if TYPE_CHECKING:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.input.base import Input
    from prompt_toolkit.output.base import Output


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
    replay_messages: list[Message] | None = None,
    replay_notice: str | None = None,
) -> int:
    """Run the prompt-toolkit interactive CLI."""
    patch_prompt_toolkit_win32_executor_shutdown()
    from prompt_toolkit import PromptSession
    from prompt_toolkit.key_binding import KeyBindings

    restored_queue = load_prompt_queue_state(active_session)
    provider_config = getattr(getattr(agent, "provider", None), "config", None)
    provider_effort = getattr(provider_config, "reasoning_effort", None)
    state = PromptCliState(
        messages=list(session_messages),
        pending_prompts=restored_queue.prompts,
        pending_images=restored_queue.pending_images,
        queue_revision=restored_queue.revision,
        queue_session_id=active_session.id,
        abandoned_turn_ids=set(),
        steered_turn_ids=set(),
        thinking_effort=(
            provider_effort
            if isinstance(provider_effort, str) and provider_effort.strip()
            else None
        ),
    )

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
    scrollback = BatchedScrollback(scrollback_console)
    session_ref: dict[str, ActiveSession] = {"active_session": active_session}
    root_label = format_root_label(active_session.root)
    tool_trace_store = ToolTraceStore()

    def estimate_toolbar_context(prompt: str = "") -> str | None:
        with state_lock:
            message_snapshot = list(state.messages)
            current_session = session_ref["active_session"]
        conversation_entries_snapshot = current_session.active_entries()
        return estimate_context_usage_text(
            agent,
            prompt,
            message_snapshot,
            conversation_entries=conversation_entries_snapshot,
        )

    def invalidate_prompt() -> None:
        app = prompt_session.app
        app.invalidate()

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
        invalidate_prompt()

    def increment_tool_count() -> None:
        with state_lock:
            state.turn_tool_count += 1
        invalidate_prompt()

    def emit_turn_summary(summary: dict[str, object]) -> None:
        text = format_turn_summary(summary)
        if text is None:
            return

        scrollback.emit("summary", text)

    context_usage_worker = ContextUsageWorker(
        state=state,
        state_lock=state_lock,
        estimate=estimate_toolbar_context,
        invalidate=invalidate_prompt,
    )
    context_usage_worker.submit("")
    renderer = PromptToolkitLiveRenderer(
        begin_tool_block=lambda: None,
        emit_tool_response_divider=lambda: scrollback.emit("divider"),
        emit_tool=lambda text, failed: scrollback.emit("tool", text, failed=failed),
        emit_agent=lambda text: scrollback.emit("agent", text),
        emit_commentary=lambda text: scrollback.emit("commentary", text),
        emit_error=lambda text: scrollback.emit("error", text),
        emit_notice=lambda text: scrollback.emit("notice", text),
        emit_warning=lambda text: scrollback.emit("warning", text),
        set_status=update_status,
        set_context_usage=update_context_usage,
        set_context_details=update_context_details,
        set_turn_tokens=update_turn_tokens,
        increment_tool_count=increment_tool_count,
        emit_turn_summary=emit_turn_summary,
        record_tool_event=tool_trace_store.record_event,
    )
    key_bindings = KeyBindings()

    show_tool_inspector, show_process_inspector = create_inspector_launchers(
        agent=agent,
        prompt_session=prompt_session,
        state=state,
        state_lock=state_lock,
        trace_store=tool_trace_store,
        scrollback=scrollback,
    )

    def preserve_editor_text(preserved_text: str) -> None:
        with state_lock:
            state.next_editor_text = preserved_text

    def attach_image(attachment: ImageAttachment) -> None:
        attach_pending_image(
            state=state,
            state_lock=state_lock,
            active_session=session_ref["active_session"],
            attachment=attachment,
        )
        update_status(f"Attached image: {attachment.label}")

    def remove_pending_image(index: int = -1) -> None:
        removed = persist_remove_pending_image(
            state=state,
            state_lock=state_lock,
            active_session=session_ref["active_session"],
            index=index,
        )
        if removed is None:
            return
        update_status(
            "Removed image attachment: "
            f"{removed.label}. Edit its prompt reference if needed."
        )
        invalidate_prompt()

    def request_clipboard_paste(buffer: object, clipboard_text: str) -> None:
        with state_lock:
            editor_revision = state.editor_revision

        def apply_result(result: ClipboardPasteResult) -> None:
            with state_lock:
                if state.editor_revision != editor_revision:
                    return
            if result.error is not None:
                update_status(f"Could not read clipboard: {result.error}")
                return
            attachment = result.attachment
            if attachment is None and result.text:
                try:
                    attachment = ImageAttachment(
                        path=resolve_image_path(
                            result.text,
                            root=session_ref["active_session"].root,
                        )
                    )
                except ValueError:
                    getattr(buffer, "insert_text")(result.text)
                    invalidate_prompt()
                    return
            if attachment is not None:
                attach_image(attachment)
                insert_attachment_reference(buffer, attachment)
                invalidate_prompt()

        def on_result(result: ClipboardPasteResult) -> None:
            loop = prompt_session.app.loop
            if loop is None or loop.is_closed():
                return
            loop.call_soon_threadsafe(apply_result, result)

        start_clipboard_paste(clipboard_text, on_result=on_result)

    control = create_prompt_toolkit_control(
        state=state,
        agent=agent,
        active_session_ref=session_ref,
        renderer=renderer,
        state_lock=state_lock,
        request_context_usage=context_usage_worker.submit,
        invalidate_prompt=invalidate_prompt,
        update_status=update_status,
        scrollback=scrollback,
        retire_tool_traces=tool_trace_store.retire_turn,
    )

    def cycle_thinking_effort() -> str:
        provider = getattr(agent, "provider", None)
        config = getattr(provider, "config", None)
        current = getattr(config, "reasoning_effort", None)
        current_model_info = getattr(provider, "current_model_info", None)
        model_info = current_model_info() if callable(current_model_info) else None
        thinking_levels = getattr(model_info, "thinking_levels", ())
        next_effort = cycle_prompt_thinking_effort(
            current,
            tuple(thinking_levels),
        )
        if config is not None and hasattr(config, "reasoning_effort"):
            config.reasoning_effort = next_effort
        invalidate_prompt()
        return next_effort

    register_prompt_toolkit_key_bindings(
        key_bindings,
        state=state,
        stop_active_turn=control.stop_active_turn,
        remove_last_image=lambda: remove_pending_image(),
        cycle_thinking_effort=cycle_thinking_effort,
        request_clipboard_paste=request_clipboard_paste,
        open_tool_inspector=show_tool_inspector,
        open_process_inspector=show_process_inspector,
        open_model_selector=preserve_editor_text,
        open_tree_selector=preserve_editor_text,
        open_queue_manager=preserve_editor_text,
        update_status=update_status,
    )
    initialize_prompt_toolkit_session(
        state=state,
        replay_session=replay_session,
        replay_messages=replay_messages,
        replay_notice=replay_notice,
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
        start_pending_prompt=control.start_pending_prompt,
        start_compaction=control.start_compaction,
        steer_active_turn=control.steer_active_turn,
        open_process_inspector=show_process_inspector,
        format_context_usage_text=format_context_usage_text,
        request_context_usage=context_usage_worker.submit,
        scrollback=scrollback,
    )
