"""ACP turn ownership and native lifecycle notification projection."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import TYPE_CHECKING, Any, cast

from acp import RequestError
from acp import schema as s

from yoke.acp.prompting import decode_prompt

if TYPE_CHECKING:
    from yoke.acp.bridge import YokeAcpAgent


async def emit(
    agent: YokeAcpAgent, session_id: str, kind: str, data: dict[str, Any]
) -> None:
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
        start = kind.endswith("started")
        update: dict[str, Any] = {
            "sessionUpdate": "tool_call" if start else "tool_call_update",
            "toolCallId": data["tool_call_id"],
            "status": "in_progress"
            if start
            else ("completed" if data["ok"] else "failed"),
        }
        if start:
            update.update(
                title=data["tool_name"],
                kind="other",
                rawInput=data.get("tool_arguments"),
            )
        else:
            update["rawOutput"] = data.get("result")
            update["content"] = [
                {
                    "type": "content",
                    "content": {
                        "type": "text",
                        "text": json.dumps(data.get("result"), ensure_ascii=False),
                    },
                }
            ]
        await agent.update(session_id, update)


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
        try:
            reason = await agent.native.turn(
                session_id,
                decoded.text,
                input_id,
                work,
                lambda event, data: emit(agent, session_id, event, data),
                attachments=decoded.attachments,
                continuation=decoded.continuation,
            )
            return s.PromptResponse(stop_reason=cast(s.StopReason, reason))
        finally:
            work["done"].set()
            agent.active.pop(session_id, None)
