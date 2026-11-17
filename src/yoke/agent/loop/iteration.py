"""Iteration and tool sequencing for a single agent run."""

from __future__ import annotations

import logging
from itertools import count
from typing import TYPE_CHECKING

from yoke.agent.interruptions import INTERRUPTED_RESPONSE_NOTE
from yoke.agent.loop.events import completed_result
from yoke.agent.loop.requests import ModelRequests
from yoke.agent.loop.tool_core import (
    cancelled_tool_result,
    is_stopped,
    prepare_tool_calls,
)
from yoke.agent.loop.tool_runner import execute_tool_calls
from yoke.agent.loop.types import (
    AfterToolCallHook,
    AgentEventHandler,
    AgentResult,
    AgentStoppedError,
    BeforeToolCallHook,
    StopRequested,
    ToolResultCheckpoint,
)
from yoke.agent.models import AgentContext, Message, ToolCall
from yoke.ai.providers.base import ProviderError

if TYPE_CHECKING:
    from yoke.agent.loop.agent import RuntimeAgent
logger = logging.getLogger(__name__)


class AgentRun:
    """Own a run's callbacks and working context until completion or interruption."""

    def __init__(
        self,
        runtime: RuntimeAgent,
        context: AgentContext,
        *,
        on_event: AgentEventHandler | None,
        stop_requested: StopRequested | None,
        before_tool_call: BeforeToolCallHook | None,
        after_tool_call: AfterToolCallHook | None,
        after_tool_result_appended: ToolResultCheckpoint | None,
    ) -> None:
        self.runtime = runtime
        self.context = context
        self.on_event = on_event
        self.stop_requested = stop_requested
        self.before_tool_call = before_tool_call
        self.after_tool_call = after_tool_call
        self.after_tool_result_appended = after_tool_result_appended
        self.requests = ModelRequests(runtime, context, on_event)

    def run(self) -> AgentResult:
        try:
            if is_stopped(self.stop_requested):
                result = self._stopped_result(iterations=0)
                self.runtime.replace_context(self.context)
                return result
            for iteration in count(1):
                result = self._run_iteration(iteration=iteration)
                if result is not None:
                    self.runtime.replace_context(self.context)
                    return result
            raise AssertionError("The unbounded iteration loop terminated.")
        except ProviderError as exc:
            exc.partial_messages = self.context.messages
            exc.partial_conversation_entries = [
                entry.model_copy(deep=True)
                for entry in self.context.conversation_log.entries
            ]
            self.runtime.replace_context(self.context)
            raise
        except Exception:
            self.runtime.replace_context(self.context)
            raise

    def _run_iteration(self, *, iteration: int) -> AgentResult | None:
        context = self.context
        on_event = self.on_event
        stop_requested = self.stop_requested
        before_tool_call = self.before_tool_call
        after_tool_call = self.after_tool_call
        after_tool_result_appended = self.after_tool_result_appended
        if is_stopped(stop_requested):
            return self._stopped_result(iterations=iteration - 1)
        if self._pre_model(iteration):
            return self._stopped_result(iterations=iteration - 1)
        assistant_message = self.requests.complete(
            iteration=iteration, stop_requested=stop_requested
        )
        self.runtime.context_manager.append_message(context, assistant_message)
        if is_stopped(stop_requested):
            self._append_cancelled_context_tool_results(
                context,
                assistant_message.tool_calls,
            )
            return self._stopped_result(iterations=iteration)
        if not assistant_message.tool_calls:
            self._sync_skills()
            return completed_result(
                context,
                output=assistant_message.final_text_content() or "",
                iterations=iteration,
                on_event=on_event,
            )
        prepared_calls = self._prepare_iteration_tool_calls(
            context,
            assistant_message.tool_calls,
            iteration=iteration,
            on_event=on_event,
            stop_requested=stop_requested,
            before_tool_call=before_tool_call,
        )
        if prepared_calls is None:
            self._append_cancelled_context_tool_results(
                context,
                assistant_message.tool_calls,
            )
            return self._stopped_result(iterations=iteration)
        if self._execute_iteration_tool_calls(
            context,
            prepared_calls,
            iteration=iteration,
            on_event=on_event,
            stop_requested=stop_requested,
            after_tool_call=after_tool_call,
            after_tool_result_appended=after_tool_result_appended,
        ) or is_stopped(stop_requested):
            return self._stopped_result(iterations=iteration)
        return None

    def _prepare_iteration_tool_calls(
        self,
        context: AgentContext,
        tool_calls: list[ToolCall],
        *,
        iteration: int,
        on_event: AgentEventHandler | None,
        stop_requested: StopRequested | None,
        before_tool_call: BeforeToolCallHook | None,
    ) -> list | None:
        try:
            return prepare_tool_calls(
                tools=self.runtime.tools,
                iteration=iteration,
                context=context,
                tool_calls=tool_calls,
                emit=lambda event, payload: on_event and on_event(event, payload),
                stop_requested=stop_requested,
                raise_if_stopped=self._raise_if_stopped,
                before_tool_call=before_tool_call,
            )
        except AgentStoppedError:
            return None

    def _execute_iteration_tool_calls(
        self,
        context: AgentContext,
        prepared_calls: list,
        *,
        iteration: int,
        on_event: AgentEventHandler | None,
        stop_requested: StopRequested | None,
        after_tool_call: AfterToolCallHook | None,
        after_tool_result_appended: ToolResultCheckpoint | None,
    ) -> bool:
        tool_results, stopped = execute_tool_calls(
            tools=self.runtime.tools,
            tool_execution=self.runtime.tool_execution,
            iteration=iteration,
            prepared_calls=prepared_calls,
            context=context,
            emit=lambda event, payload: on_event and on_event(event, payload),
            stop_requested=stop_requested,
            after_tool_call=after_tool_call,
        )
        # Provider tool-call batches must be closed by every matching tool result
        # before any non-tool provider message can be appended. Tool-injected
        # context such as attach_image uses a user-role message, so interleaving
        # it after the first result of a multi-call batch leaves the remaining
        # calls open and violates the provider/tool sequence.
        for tool_call, arguments, result, projection in tool_results:
            self.runtime.context_manager.append_tool_result(
                context,
                tool_call_id=tool_call.id,
                result=result,
                provider_result_projection=projection,
            )

        # Persist the valid, fully-closed tool batch before constructing any
        # additional provider-visible context. This also leaves a resumable
        # checkpoint if tool-context construction itself fails.
        if tool_results and after_tool_result_appended is not None:
            after_tool_result_appended(context)

        for tool_call, arguments, result, _ in tool_results:
            self._append_tool_context_messages(
                context,
                tool_name=tool_call.function.name,
                tool_call_id=tool_call.id,
                arguments=arguments,
                result=result,
            )

        # Keep injected context durable too. The callback name is historical;
        # the checkpoint represents the complete post-tool batch boundary.
        if tool_results and after_tool_result_appended is not None:
            after_tool_result_appended(context)
        if tool_results:
            self._post_tools(iteration)
        return stopped

    def _append_tool_context_messages(
        self,
        context: AgentContext,
        *,
        tool_name: str,
        tool_call_id: str,
        arguments: dict[str, object],
        result: dict[str, object],
    ) -> None:
        tool = self.runtime.tools.get(tool_name)
        if tool is None or not hasattr(tool, "pending_context_messages"):
            return
        try:
            invocation = tool.parse_arguments(arguments)
        except Exception:
            # Invalid arguments are a model mistake already reported in the
            # tool result, not an internal failure.
            logger.debug("Skipping pending context for tool %s", tool_name)
            return
        try:
            pending = invocation.pending_context_messages(result)
        except Exception:
            logger.warning(
                "Could not collect pending context for tool %s",
                tool_name,
                exc_info=True,
            )
            return
        for message in pending:
            if message.role == "system":
                self.runtime.context_manager.append_skill_message(context, message)
            else:
                self.runtime.context_manager.append_tool_context_message(
                    context,
                    message,
                    tool_name=tool_name,
                    tool_call_id=tool_call_id,
                )

    def _append_cancelled_context_tool_results(
        self,
        context: AgentContext,
        tool_calls: list[ToolCall],
    ) -> None:
        for tool_call in tool_calls:
            self.runtime.context_manager.append_tool_result(
                context,
                tool_call_id=tool_call.id,
                result=cancelled_tool_result(),
            )

    def _raise_if_stopped(self, stop_requested: StopRequested | None) -> None:
        if is_stopped(stop_requested):
            raise AgentStoppedError("Agent stopped")

    def _stopped_result(self, *, iterations: int) -> AgentResult:
        """Build the standard stopped result payload."""
        context = self.context
        self.runtime.context_manager.append_message(
            context, Message.system(INTERRUPTED_RESPONSE_NOTE)
        )
        return AgentResult(
            output="Stopped current turn.",
            messages=context.messages,
            iterations=iterations,
            status="stopped",
            conversation_entries=[
                entry.model_copy(deep=True)
                for entry in context.conversation_log.entries
            ],
        )

    def _pre_model(self, iteration: int) -> bool:
        self.runtime.command_notices.append(self.context)
        compaction = self.requests.compact(
            iteration=iteration, stop_requested=self.stop_requested
        )
        if compaction.failed:
            return True
        if compaction.result is not None:
            self.requests.emit_usage(iteration=iteration, reason="compaction")
        return False

    def _post_tools(self, iteration: int) -> None:
        self.runtime.active_skills = [
            skill.model_copy(deep=True) for skill in self.context.active_skills
        ]
        if self.runtime.refresh_tools(force=True):
            self.runtime.sync_context_instructions(self.context)
        self.requests.emit_usage(iteration=iteration, reason="tool_results")
        compaction = self.requests.compact(
            iteration=iteration,
            after_tool_results=True,
            stop_requested=self.stop_requested,
        )
        if compaction.failed:
            raise AgentStoppedError()
        if compaction.result is not None:
            self.requests.emit_usage(iteration=iteration, reason="post_tool_compaction")

    def _sync_skills(self) -> None:
        changed = self.runtime.active_skills != self.context.active_skills
        self.runtime.active_skills = [
            skill.model_copy(deep=True) for skill in self.context.active_skills
        ]
        if changed and self.runtime.refresh_tools(force=True):
            self.runtime.sync_context_instructions(self.context)
