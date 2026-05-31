"""Tool execution runners for the agent loop."""

from __future__ import annotations

from concurrent.futures import FIRST_COMPLETED
from concurrent.futures import Future
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import wait

from yoke.agent.loop.tool_core import cancelled_tool_result
from yoke.agent.loop.tool_core import execute_tool
from yoke.agent.loop.tool_core import finalize_tool_result
from yoke.agent.loop.tool_core import is_stopped
from yoke.agent.loop.types import AfterToolCallHook
from yoke.agent.loop.types import ImmediateToolResult
from yoke.agent.loop.types import PreparedToolCall
from yoke.agent.loop.types import StopRequested
from yoke.agent.models import AgentContext
from yoke.agent.models import ToolCall
from yoke.agent.tools import LocalTool


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
    if tool_execution == "sequential" or len(runnable) < 2:
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
        raw_result = execute_tool(
            tools,
            prepared.tool_call.function.name,
            prepared.arguments,
            cancel_requested=stop_requested,
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
    executor = ThreadPoolExecutor(max_workers=len(runnable))
    future_pairs: dict[Future[dict[str, object]], PreparedToolCall] = {}
    stopped = False
    try:
        for prepared in runnable:
            future_pairs[
                executor.submit(
                    execute_tool,
                    tools,
                    prepared.tool_call.function.name,
                    prepared.arguments,
                    cancel_requested=stop_requested,
                )
            ] = prepared
        pending = set(future_pairs)
        while pending:
            if is_stopped(stop_requested):
                stopped = True
                done = {future for future in pending if future.done()}
            else:
                done, pending = wait(
                    pending,
                    timeout=0.05,
                    return_when=FIRST_COMPLETED,
                )
            for future in done:
                pending.discard(future)
                prepared = future_pairs[future]
                finalized = finalize_tool_result(
                    tools=tools,
                    iteration=iteration,
                    tool_call=prepared.tool_call,
                    arguments=prepared.arguments,
                    result=future.result(),
                    context=context,
                    emit=emit,
                    after_tool_call=after_tool_call,
                )
                results.append((prepared.tool_call, prepared.arguments, finalized))
                completed_ids.add(prepared.tool_call.id)
            if stopped:
                for future in pending:
                    future.cancel()
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
    finally:
        executor.shutdown(wait=not stopped, cancel_futures=True)
    return order_results(prepared_calls, results), False
