"""ACP turn ownership and native lifecycle notification projection."""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import quote

from acp import RequestError
from acp import schema as s

from yoke.acp.prompting import decode_prompt
from yoke.acp.tools import ToolCallProjector

if TYPE_CHECKING:
    from yoke.acp.bridge import YokeAcpAgent


async def emit(
    agent: YokeAcpAgent,
    session_id: str,
    kind: str,
    data: dict[str, Any],
    tools: ToolCallProjector,
) -> None:
    if kind == "session.tool.reconcile":
        call_id = data["id"]
        if tools.has_completed(call_id):
            return
        if not tools.has_started(call_id):
            await emit(
                agent,
                session_id,
                "session.tool.started",
                {
                    "tool_call_id": call_id,
                    "tool_name": data["name"],
                    "tool_arguments": data["arguments"],
                },
                tools,
            )
        # Live tool events are ephemeral. The settled transcript supplies exact
        # call ownership, and the inspector supplies the actual raw result.
        try:
            info = await agent.native.data(
                "GET",
                agent.native.path(session_id, "/tool-call/" + quote(call_id, safe="")),
            )
        except Exception:  # noqa: BLE001 - finish() marks an unavailable result
            return
        if info.get("id") != call_id or info.get("status") not in (
            "ok",
            "failed",
            "cancelled",
        ):
            return
        await emit(
            agent,
            session_id,
            "session.tool.ended",
            {
                "tool_call_id": call_id,
                "tool_name": info["toolName"],
                "executed_arguments": info.get("arguments", {}).get("executed"),
                "result": info.get("result"),
                "ok": info["status"] == "ok",
            },
            tools,
        )
        return
    if kind == "answer" or (
        kind == "session.message.updated" and data.get("phase") == "commentary"
    ):
        await agent.update(
            session_id,
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": data["content"]},
            },
        )
    elif kind in ("session.tool.started", "session.tool.ended"):
        call_id = data["tool_call_id"]
        if tools.has_completed(call_id) or (
            kind == "session.tool.started" and tools.has_started(call_id)
        ):
            return
        await agent.update(
            session_id, tools.update(start=kind.endswith("started"), data=data)
        )
        result = data.get("result")
        if (
            kind == "session.tool.ended"
            and data.get("tool_name")
            in ("command_exec", "python_exec", "process_read", "process_input")
            and isinstance(result, dict)
        ):
            processes = (
                result.get("items", [])
                if data["tool_name"] == "process_read"
                else [result]
            )
            if isinstance(processes, list):
                for process in processes:
                    if (
                        isinstance(process, dict)
                        and process.get("running") is True
                        and type(process.get("session_id")) is int
                    ):
                        await agent.native.process_monitor.track(
                            session_id,
                            process["session_id"],
                            data["tool_call_id"],
                            agent.update,
                        )


async def prompt(
    agent: YokeAcpAgent, session_id: str, blocks: list[Any], kwargs: dict[str, Any]
) -> s.PromptResponse:
    agent.known(session_id)
    meta = kwargs.get("_meta") or {}
    if not isinstance(meta, dict):
        raise RequestError(-32602, "Invalid prompt metadata")
    # ACP's router unpacks request _meta into keyword arguments. Also accept
    # the explicit mapping used by direct callers, without relaxing the marker.
    meta = {"yokeContinuation": kwargs.get("yokeContinuation", False), **meta}
    decoded = decode_prompt(
        blocks,
        meta,
        images=agent.catalogs[session_id][agent.sessions[session_id]["model"]]["images"]
        is True,
    )
    input_id = meta.get("yokeInputID", kwargs.get("yokeInputID", str(uuid.uuid4())))
    if not isinstance(input_id, str) or not input_id.strip():
        raise RequestError(-32602, "yokeInputID must be a nonempty string")
    async with agent.exclusive(session_id):
        work = {
            "cancel": False,
            "wake": asyncio.Event(),
            "done": asyncio.Event(),
            "task": asyncio.current_task(),
        }
        agent.active[session_id] = work
        tools = ToolCallProjector()
        reason = "failed"
        try:
            session = await agent.native.data("GET", agent.native.path(session_id))
            tools.cwd = session.get("location", {}).get("directory")
            reason = await agent.native.turn(
                session_id,
                decoded.text,
                input_id,
                work,
                lambda event, data: emit(agent, session_id, event, data, tools),
                attachments=decoded.attachments,
                continuation=decoded.continuation,
            )
            return s.PromptResponse(stop_reason=cast(s.StopReason, reason))
        finally:
            try:
                for update in tools.finish(
                    interrupted=reason == "cancelled" or work["cancel"]
                ):
                    try:
                        await agent.update(session_id, update)
                    except Exception:  # noqa: BLE001 - preserve the original turn error
                        break
            finally:
                work["done"].set()
                agent.active.pop(session_id, None)
