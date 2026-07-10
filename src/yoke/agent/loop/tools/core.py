"""Core tool parsing and finalization helpers."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace

from pydantic import ValidationError

from yoke.agent.loop.types import AfterToolCallContext
from yoke.agent.loop.types import AfterToolCallHook
from yoke.agent.loop.types import BeforeToolCallContext
from yoke.agent.loop.types import BeforeToolCallHook
from yoke.agent.loop.types import ImmediateToolResult
from yoke.agent.loop.types import PreparedToolCall
from yoke.agent.loop.types import StopRequested
from yoke.agent.models import AgentContext
from yoke.agent.models import ToolCall
from yoke.agent.tools import LocalTool
from yoke.agent.tools import ToolRuntimeContext


def index_tools(tools: Sequence[LocalTool]) -> dict[str, LocalTool]:
    """Index tools by name and reject duplicates."""
    duplicate_names = sorted(
        {
            tool.name
            for tool in tools
            if sum(1 for candidate in tools if candidate.name == tool.name) > 1
        }
    )
    if duplicate_names:
        joined = ", ".join(repr(name) for name in duplicate_names)
        raise ValueError(f"Duplicate tool names are not allowed: {joined}")
    return {tool.name: tool for tool in tools}


def tool_definitions(tools: dict[str, LocalTool]) -> list[dict[str, object]]:
    """Return provider tool definitions."""
    return [tool.to_definition() for tool in tools.values()]


def prepare_tool(
    tools: dict[str, LocalTool],
    name: str,
    arguments: dict[str, object],
) -> LocalTool:
    """Parse arguments for a named tool."""
    tool = tools.get(name)
    if tool is None:
        raise KeyError(f"Unknown tool: {name}")
    return tool.parse_arguments(arguments)


def parse_tool_arguments(tool_call: ToolCall) -> dict[str, object]:
    """Parse tool arguments from a provider tool call."""
    try:
        arguments = json.loads(tool_call.function.arguments)
    except json.JSONDecodeError as exc:
        if tool_call.function.name == "apply_patch":
            return {"input": tool_call.function.arguments}
        raise ValueError(f"Invalid tool arguments: {exc}") from exc
    if isinstance(arguments, dict):
        return arguments
    if tool_call.function.name == "apply_patch" and isinstance(arguments, str):
        return {"input": arguments}
    raise ValueError("Tool arguments must decode to an object")


def execute_tool(
    tools: dict[str, LocalTool],
    name: str,
    arguments: dict[str, object],
    *,
    cancel_requested: StopRequested | None = None,
    tool_event=None,
) -> dict[str, object]:
    """Execute a named tool with parsed arguments."""
    try:
        invocation = prepare_tool(tools, name, arguments)
        if cancel_requested is not None:
            invocation._context = {
                **invocation._context,
                "cancel_requested": cancel_requested,
            }
            runtime_context = invocation._context.get("runtime_context")
            if isinstance(runtime_context, ToolRuntimeContext):
                invocation.bind_runtime_context(
                    replace(
                        runtime_context,
                        cancel_requested=cancel_requested,
                    )
                )
        if tool_event is not None:
            invocation._context = {**invocation._context, "tool_event": tool_event}
            runtime_context = invocation._context.get("runtime_context")
            if isinstance(runtime_context, ToolRuntimeContext):
                invocation.bind_runtime_context(
                    replace(runtime_context, tool_event=tool_event)
                )
        return invocation.execute()
    except KeyError as exc:
        return {"ok": False, "error": str(exc)}
    except ValidationError as exc:
        return {"ok": False, "error": f"Invalid tool payload: {exc}"}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def prepare_tool_calls(
    *,
    tools: dict[str, LocalTool],
    iteration: int,
    context: AgentContext,
    tool_calls: list[ToolCall],
    emit,
    stop_requested: StopRequested | None,
    raise_if_stopped,
    before_tool_call: BeforeToolCallHook | None,
) -> list[PreparedToolCall | ImmediateToolResult]:
    """Prepare tool invocations and apply pre-execution hooks."""
    prepared: list[PreparedToolCall | ImmediateToolResult] = []
    for tool_call in tool_calls:
        raise_if_stopped(stop_requested)
        emit(
            "tool_execution_start",
            {
                "iteration": iteration,
                "tool_name": tool_call.function.name,
                "tool_call_id": tool_call.id,
                "tool_arguments": tool_call.function.arguments,
            },
        )
        try:
            parsed_arguments = parse_tool_arguments(tool_call)
            invocation = prepare_tool(tools, tool_call.function.name, parsed_arguments)
            arguments = invocation.model_dump(by_alias=True, exclude_none=True)
            if before_tool_call is not None:
                hook_result = before_tool_call(
                    BeforeToolCallContext(
                        iteration=iteration,
                        tool_call=tool_call,
                        arguments=dict(arguments),
                        context=context,
                    )
                )
                if hook_result is not None and hook_result.block:
                    prepared.append(
                        ImmediateToolResult(
                            tool_call=tool_call,
                            result={
                                "ok": False,
                                "error": hook_result.reason
                                or "Tool execution was blocked",
                            },
                        )
                    )
                    continue
                if hook_result and hook_result.arguments is not None:
                    invocation = prepare_tool(
                        tools,
                        tool_call.function.name,
                        hook_result.arguments,
                    )
                    arguments = invocation.model_dump(by_alias=True, exclude_none=True)
            prepared.append(PreparedToolCall(tool_call=tool_call, arguments=arguments))
        except Exception as exc:
            prepared.append(
                ImmediateToolResult(
                    tool_call=tool_call,
                    result={"ok": False, "error": str(exc)},
                )
            )
    return prepared


def finalize_tool_result(
    *,
    tools: dict[str, LocalTool],
    iteration: int,
    tool_call: ToolCall,
    arguments: dict[str, object],
    result: dict[str, object],
    context: AgentContext,
    emit,
    after_tool_call: AfterToolCallHook | None,
) -> dict[str, object]:
    """Apply post-processing to a tool result and emit completion."""
    finalized = dict(result)
    try:
        invocation = prepare_tool(tools, tool_call.function.name, arguments)
        invocation.apply_result(context, finalized)
    except Exception as exc:
        finalized["ok"] = False
        finalized["error"] = f"Tool result application failed: {exc}"
    if after_tool_call is not None:
        try:
            hook_result = after_tool_call(
                AfterToolCallContext(
                    iteration=iteration,
                    tool_call=tool_call,
                    arguments=arguments,
                    result=dict(finalized),
                    context=context,
                )
            )
            if hook_result is not None and hook_result.result is not None:
                finalized = hook_result.result
        except Exception as exc:
            finalized["ok"] = False
            finalized["error"] = f"after_tool_call hook failed: {exc}"
    emit(
        "tool_execution_end",
        {
            "iteration": iteration,
            "tool_name": tool_call.function.name,
            "tool_call_id": tool_call.id,
            "ok": finalized.get("ok", False),
            "result": finalized,
            "executed_arguments": arguments,
        },
    )
    return finalized


def cancelled_tool_result() -> dict[str, object]:
    """Return the standard cancelled tool result payload."""
    return {
        "ok": False,
        "cancelled": True,
        "error": "Tool call cancelled because the turn was stopped",
    }


def is_stopped(stop_requested: StopRequested | None) -> bool:
    """Return whether the current run has been stopped."""
    return bool(stop_requested is not None and stop_requested())
