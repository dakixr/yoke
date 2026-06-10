"""Runtime agent iteration helpers."""

from __future__ import annotations

from yoke.agent.context import ContextManager
from yoke.agent.loop.lifecycle import complete_iteration_model
from yoke.agent.loop.lifecycle import completed_result
from yoke.agent.loop.lifecycle import handle_post_tool_results
from yoke.agent.loop.lifecycle import handle_pre_model_compaction
from yoke.agent.loop.lifecycle import stopped_result
from yoke.agent.loop.lifecycle import sync_runtime_skills_from_context
from yoke.agent.loop.tool_core import cancelled_tool_result
from yoke.agent.loop.tool_core import is_stopped
from yoke.agent.loop.tool_core import prepare_tool_calls
from yoke.agent.loop.tool_core import tool_definitions
from yoke.agent.loop.tool_runner import execute_tool_calls
from yoke.agent.loop.types import AfterToolCallHook
from yoke.agent.loop.types import AgentEventHandler
from yoke.agent.loop.types import AgentResult
from yoke.agent.loop.types import AgentStoppedError
from yoke.agent.loop.types import BeforeToolCallHook
from yoke.agent.loop.types import StopRequested
from yoke.agent.loop.types import ToolExecutionMode
from yoke.agent.loop.types import ToolResultCheckpoint
from yoke.agent.models import AgentContext
from yoke.agent.models import ToolCall
from yoke.agent.tools import LocalTool


class RuntimeAgentIterationMixin:
    """Private iteration behavior for RuntimeAgent."""

    context_manager: ContextManager
    tools: dict[str, LocalTool]
    tool_execution: ToolExecutionMode

    def _run_iteration(
        self,
        context: AgentContext,
        *,
        iteration: int,
        on_event: AgentEventHandler | None,
        stop_requested: StopRequested | None,
        before_tool_call: BeforeToolCallHook | None,
        after_tool_call: AfterToolCallHook | None,
        after_tool_result_appended: ToolResultCheckpoint | None = None,
    ) -> AgentResult | None:
        if self._is_stopped(stop_requested):
            return self._stopped_result(context, iterations=iteration - 1)
        if handle_pre_model_compaction(self, context, iteration, on_event):
            return self._stopped_result(context, iterations=iteration - 1)
        assistant_message = complete_iteration_model(
            self,
            context,
            iteration=iteration,
            on_event=on_event,
        )
        self.context_manager.append_message(context, assistant_message)
        if self._is_stopped(stop_requested):
            self._append_cancelled_context_tool_results(
                context,
                assistant_message.tool_calls,
            )
            return self._stopped_result(context, iterations=iteration)
        if not assistant_message.tool_calls:
            sync_runtime_skills_from_context(self, context)
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
            return self._stopped_result(context, iterations=iteration)
        if self._execute_iteration_tool_calls(
            context,
            prepared_calls,
            iteration=iteration,
            on_event=on_event,
            stop_requested=stop_requested,
            after_tool_call=after_tool_call,
            after_tool_result_appended=after_tool_result_appended,
        ) or self._is_stopped(stop_requested):
            return self._stopped_result(context, iterations=iteration)
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
                tools=self.tools,
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
            tools=self.tools,
            tool_execution=self.tool_execution,
            iteration=iteration,
            prepared_calls=prepared_calls,
            context=context,
            emit=lambda event, payload: on_event and on_event(event, payload),
            stop_requested=stop_requested,
            after_tool_call=after_tool_call,
        )
        for tool_call, arguments, result in tool_results:
            self.context_manager.append_tool_result(
                context,
                tool_call_id=tool_call.id,
                result=result,
            )
            self._append_tool_context_messages(
                context,
                tool_name=tool_call.function.name,
                arguments=arguments,
                result=result,
            )
            if after_tool_result_appended is not None:
                after_tool_result_appended(context)
        if tool_results:
            handle_post_tool_results(self, context, iteration, on_event)
        return stopped

    def _append_tool_context_messages(
        self,
        context: AgentContext,
        *,
        tool_name: str,
        arguments: dict[str, object],
        result: dict[str, object],
    ) -> None:
        tool = self.tools.get(tool_name)
        if tool is None or not hasattr(tool, "pending_context_messages"):
            return
        try:
            invocation = tool.parse_arguments(arguments)
            pending = invocation.pending_context_messages(result)
        except Exception:
            return
        for message in pending:
            self.context_manager.append_message(context, message)

    def _append_cancelled_context_tool_results(
        self,
        context: AgentContext,
        tool_calls: list[ToolCall],
    ) -> None:
        for tool_call in tool_calls:
            self.context_manager.append_tool_result(
                context,
                tool_call_id=tool_call.id,
                result=cancelled_tool_result(),
            )

    def _raise_if_stopped(self, stop_requested: StopRequested | None) -> None:
        if self._is_stopped(stop_requested):
            raise AgentStoppedError("Agent stopped")

    def _is_stopped(self, stop_requested: StopRequested | None) -> bool:
        return is_stopped(stop_requested)

    def _tool_definitions(self) -> list[dict[str, object]]:
        return tool_definitions(self.tools)

    def _stopped_result(
        self,
        context: AgentContext,
        *,
        iterations: int,
    ) -> AgentResult:
        return stopped_result(
            context,
            iterations=iterations,
            append_message=self.context_manager.append_message,
        )
