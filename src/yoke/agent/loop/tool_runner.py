"""Tool execution runners for the agent loop."""

from __future__ import annotations

import time

from yoke.agent.loop.tool_core import cancelled_tool_result
from yoke.agent.loop.tool_core import finalize_tool_result
from yoke.agent.loop.tool_core import is_stopped
from yoke.agent.loop.tool_process import ToolProcessInvocation
from yoke.agent.loop.tool_process import TOOL_POLL_SECONDS
from yoke.agent.loop.tool_process import wait_for_tool_process
from yoke.agent.loop.types import AfterToolCallHook
from yoke.agent.loop.types import ImmediateToolResult
from yoke.agent.loop.types import PreparedToolCall
from yoke.agent.loop.types import StopRequested
from yoke.agent.models import AgentContext
from yoke.agent.models import ToolCall
from yoke.agent.tools import LocalTool


def _execute_tool_call(
    *,
    tools: dict[str, LocalTool],
    prepared: PreparedToolCall,
    context: AgentContext,
    stop_requested: StopRequested | None,
) -> tuple[dict[str, object], bool]:
    tool = tools.get(prepared.tool_call.function.name)
    if tool is not None and tool.execute_in_process:
        from yoke.agent.loop.tool_core import execute_tool

        tool._context["messages"] = list(context.messages)
        return execute_tool(
            tools,
            prepared.tool_call.function.name,
            prepared.arguments,
            cancel_requested=stop_requested,
        ), is_stopped(stop_requested)
    invocation = ToolProcessInvocation(
        tools=tools,
        name=prepared.tool_call.function.name,
        arguments=prepared.arguments,
    )
    try:
        invocation.start()
        return wait_for_tool_process(
            invocation,
            stop_requested=stop_requested,
        )
    except BaseException:
        invocation.cancel()
        raise


def execute_tool_calls(
    *,
    tools: dict[str, LocalTool],
    tool_execution: str,
    iteration: int,
    prepared_calls: list[PreparedToolCall | ImmediateToolResult],
    context: AgentContext,
    emit,
    stop_requested: StopRequested | None,
    after_tool_call: AfterToolCallHook | None,
) -> tuple[list[tuple[ToolCall, dict[str, object], dict[str, object]]], bool]:
    """Execute prepared tool calls and finalize results."""
    results: list[tuple[ToolCall, dict[str, object], dict[str, object]]] = []
    completed_ids: set[str] = set()
    runnable = [item for item in prepared_calls if isinstance(item, PreparedToolCall)]
    immediate = [
        item for item in prepared_calls if isinstance(item, ImmediateToolResult)
    ]
    for item in immediate:
        if is_stopped(stop_requested):
            append_cancelled_tool_results(
                prepared_calls=prepared_calls,
                results=results,
                completed_ids=completed_ids,
                iteration=iteration,
                context=context,
                emit=emit,
                after_tool_call=after_tool_call,
                tools=tools,
            )
            return order_results(prepared_calls, results), True
        finalized = finalize_tool_result(
            tools=tools,
            iteration=iteration,
            tool_call=item.tool_call,
            arguments={},
            result=item.result,
            context=context,
            emit=emit,
            after_tool_call=after_tool_call,
        )
        results.append((item.tool_call, {}, finalized))
        completed_ids.add(item.tool_call.id)
    if (
        tool_execution == "sequential"
        or len(runnable) < 2
        or any(
            tools.get(item.tool_call.function.name) is not None
            and tools[item.tool_call.function.name].execute_in_process
            for item in runnable
        )
    ):
        return _execute_sequential(
            tools=tools,
            prepared_calls=prepared_calls,
            runnable=runnable,
            results=results,
            completed_ids=completed_ids,
            iteration=iteration,
            context=context,
            emit=emit,
            stop_requested=stop_requested,
            after_tool_call=after_tool_call,
        )
    return _execute_parallel(
        tools=tools,
        prepared_calls=prepared_calls,
        runnable=runnable,
        results=results,
        completed_ids=completed_ids,
        iteration=iteration,
        context=context,
        emit=emit,
        stop_requested=stop_requested,
        after_tool_call=after_tool_call,
    )


def append_cancelled_tool_results(
    *,
    prepared_calls: list[PreparedToolCall | ImmediateToolResult],
    results: list[tuple[ToolCall, dict[str, object], dict[str, object]]],
    completed_ids: set[str],
    iteration: int,
    context: AgentContext,
    emit,
    after_tool_call: AfterToolCallHook | None,
    tools: dict[str, LocalTool],
) -> None:
    """Append cancellation results for unfinished tool calls."""
    for item in prepared_calls:
        if item.tool_call.id in completed_ids:
            continue
        arguments = item.arguments if isinstance(item, PreparedToolCall) else {}
        finalized = finalize_tool_result(
            tools=tools,
            iteration=iteration,
            tool_call=item.tool_call,
            arguments=arguments,
            result=cancelled_tool_result(),
            context=context,
            emit=emit,
            after_tool_call=after_tool_call,
        )
        results.append((item.tool_call, arguments, finalized))
        completed_ids.add(item.tool_call.id)


def order_results(
    prepared_calls: list[PreparedToolCall | ImmediateToolResult],
    results: list[tuple[ToolCall, dict[str, object], dict[str, object]]],
) -> list[tuple[ToolCall, dict[str, object], dict[str, object]]]:
    """Return tool results in original provider order."""
    ordered = {
        tool_call.id: (tool_call, arguments, result)
        for tool_call, arguments, result in results
    }
    return [
        ordered[item.tool_call.id]
        for item in prepared_calls
        if item.tool_call.id in ordered
    ]


def _execute_sequential(
    *,
    tools: dict[str, LocalTool],
    prepared_calls: list[PreparedToolCall | ImmediateToolResult],
    runnable: list[PreparedToolCall],
    results: list[tuple[ToolCall, dict[str, object], dict[str, object]]],
    completed_ids: set[str],
    iteration: int,
    context: AgentContext,
    emit,
    stop_requested: StopRequested | None,
    after_tool_call: AfterToolCallHook | None,
) -> tuple[list[tuple[ToolCall, dict[str, object], dict[str, object]]], bool]:
    for prepared in runnable:
        if is_stopped(stop_requested):
            append_cancelled_tool_results(
                prepared_calls=prepared_calls,
                results=results,
                completed_ids=completed_ids,
                iteration=iteration,
                context=context,
                emit=emit,
                after_tool_call=after_tool_call,
                tools=tools,
            )
            return order_results(prepared_calls, results), True
        raw_result, stopped = _execute_tool_call(
            tools=tools,
            prepared=prepared,
            context=context,
            stop_requested=stop_requested,
        )
        finalized = finalize_tool_result(
            tools=tools,
            iteration=iteration,
            tool_call=prepared.tool_call,
            arguments=prepared.arguments,
            result=raw_result,
            context=context,
            emit=emit,
            after_tool_call=after_tool_call,
        )
        results.append((prepared.tool_call, prepared.arguments, finalized))
        completed_ids.add(prepared.tool_call.id)
        if stopped:
            append_cancelled_tool_results(
                prepared_calls=prepared_calls,
                results=results,
                completed_ids=completed_ids,
                iteration=iteration,
                context=context,
                emit=emit,
                after_tool_call=after_tool_call,
                tools=tools,
            )
            return order_results(prepared_calls, results), True
    return order_results(prepared_calls, results), False


def _execute_parallel(
    *,
    tools: dict[str, LocalTool],
    prepared_calls: list[PreparedToolCall | ImmediateToolResult],
    runnable: list[PreparedToolCall],
    results: list[tuple[ToolCall, dict[str, object], dict[str, object]]],
    completed_ids: set[str],
    iteration: int,
    context: AgentContext,
    emit,
    stop_requested: StopRequested | None,
    after_tool_call: AfterToolCallHook | None,
) -> tuple[list[tuple[ToolCall, dict[str, object], dict[str, object]]], bool]:
    invocation_pairs: list[tuple[ToolProcessInvocation, PreparedToolCall]] = []
    try:
        for prepared in runnable:
            invocation = ToolProcessInvocation(
                tools=tools,
                name=prepared.tool_call.function.name,
                arguments=prepared.arguments,
            )
            invocation.start()
            invocation_pairs.append((invocation, prepared))
        pending = dict(invocation_pairs)
        while pending:
            if is_stopped(stop_requested):
                for invocation in pending:
                    invocation.cancel()
                append_cancelled_tool_results(
                    prepared_calls=prepared_calls,
                    results=results,
                    completed_ids=completed_ids,
                    iteration=iteration,
                    context=context,
                    emit=emit,
                    after_tool_call=after_tool_call,
                    tools=tools,
                )
                return order_results(prepared_calls, results), True
            else:
                done = [invocation for invocation in pending if invocation.done()]
            if not done:
                time.sleep(TOOL_POLL_SECONDS)
                continue
            for invocation in done:
                prepared = pending.pop(invocation)
                raw_result = invocation.result()
                finalized = finalize_tool_result(
                    tools=tools,
                    iteration=iteration,
                    tool_call=prepared.tool_call,
                    arguments=prepared.arguments,
                    result=raw_result,
                    context=context,
                    emit=emit,
                    after_tool_call=after_tool_call,
                )
                results.append((prepared.tool_call, prepared.arguments, finalized))
                completed_ids.add(prepared.tool_call.id)
    finally:
        for invocation, _ in invocation_pairs:
            invocation.cancel()
    return order_results(prepared_calls, results), False
