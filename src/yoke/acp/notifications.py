"""Standard ACP session metadata and interactive requests over native events."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, TYPE_CHECKING

from acp import schema as s

if TYPE_CHECKING:
    from yoke.acp.bridge import YokeAcpAgent

LOGGER = logging.getLogger(__name__)


class SessionNotifications:
    def __init__(self, agent: YokeAcpAgent) -> None:
        self.agent = agent
        self.sessions: set[str] = set()
        self.sent: dict[tuple[str, str], dict[str, Any]] = {}
        self.task: asyncio.Task[None] | None = None
        self.requests: dict[tuple[str, str], asyncio.Task[None]] = {}
        self.compactions: dict[str, str] = {}
        self.roots: dict[str, str] = {}

    async def attach(self, session_id: str) -> None:
        self.sessions.add(session_id)
        await self.refresh(session_id, commands=True)
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self.watch())

    async def detach(self, session_id: str) -> None:
        self.sessions.discard(session_id)
        self.roots.pop(session_id, None)
        self.sent = {
            key: value for key, value in self.sent.items() if key[0] != session_id
        }
        for key, task in list(self.requests.items()):
            if key[0] == session_id:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        for session_id in list(self.sessions):
            await self.detach(session_id)

    async def publish(self, session_id: str, update: dict[str, Any]) -> None:
        key = (session_id, update["sessionUpdate"])
        if self.sent.get(key) != update:
            await self.agent.update(session_id, update)
            self.sent[key] = update

    async def refresh(self, session_id: str, *, commands: bool = False) -> None:
        data = await self.agent.native.data("GET", self.agent.native.path(session_id))
        self.roots[session_id] = data["location"]["directory"]
        await self.publish(
            session_id,
            {
                "sessionUpdate": "session_info_update",
                "title": data.get("title"),
                "updatedAt": data.get("time", {}).get("updated"),
            },
        )
        await self.usage(session_id, data.get("contextUsage") or {})
        if session_id in self.agent.sessions:
            before = self.agent.options(session_id)
            self.agent.remember(
                session_id,
                data,
                self.agent.catalogs[session_id],
                self.agent.default_model or self.agent.sessions[session_id]["model"],
            )
            if before != self.agent.options(session_id):
                await self.publish(
                    session_id,
                    {
                        "sessionUpdate": "config_option_update",
                        "configOptions": self.agent.options(session_id),
                    },
                )
        client = await self.agent.native.data(
            "GET", self.agent.native.path(session_id, "/client")
        )
        if client and client.get("plan") is not None:
            await self.event(session_id, "session.plan.updated", client["plan"])
        pending: set[tuple[str, str]] = set()
        for resource in ("permission", "question"):
            for item in await self.agent.native.data(
                "GET", self.agent.native.path(session_id, "/" + resource)
            ):
                pending.add((session_id, item["id"]))
                await self.event(session_id, "session." + resource + ".requested", item)
        for key, task in list(self.requests.items()):
            if key[0] == session_id and key not in pending:
                task.cancel()
        if commands:
            from yoke.acp.commands import available_commands

            skills = await self.agent.native.skills(data["location"]["directory"])
            await self.publish(
                session_id,
                {
                    "sessionUpdate": "available_commands_update",
                    "availableCommands": available_commands(skills),
                },
            )

    async def usage(self, session_id: str, data: dict[str, Any]) -> None:
        used = data.get("input_tokens", data.get("estimated_input_tokens"))
        size = data.get("max_total_tokens")
        if type(used) is int and type(size) is int and used >= 0 and size > 0:
            await self.publish(
                session_id,
                {"sessionUpdate": "usage_update", "used": used, "size": size},
            )

    async def event(self, session_id: str, kind: str, data: dict[str, Any]) -> None:
        if kind == "session.context.updated":
            await self.usage(session_id, data)
        elif kind in {"session.updated", "session.selection.changed"}:
            await self.refresh(session_id)
        elif kind == "session.plan.updated":
            await self.publish(
                session_id,
                {
                    "sessionUpdate": "plan",
                    "entries": [
                        {
                            "content": step["step"],
                            "status": step["status"],
                            "priority": "medium",
                        }
                        for step in data["steps"]
                    ],
                },
            )
        elif kind.startswith("session.compaction."):
            started = kind.endswith("started")
            ended = kind.endswith(("ended", "failed"))
            if started:
                self.compactions[session_id] = "yoke-compaction:" + str(
                    data.get("operationID", data.get("inputID", "automatic"))
                )
            call_id = self.compactions.get(session_id, "yoke-compaction:automatic")
            await self.agent.update(
                session_id,
                {
                    "sessionUpdate": "tool_call" if started else "tool_call_update",
                    "toolCallId": call_id,
                    "title": "Compact conversation",
                    "kind": "think",
                    "status": "failed"
                    if kind.endswith("failed")
                    else "completed"
                    if ended
                    else "in_progress",
                    "rawOutput": data,
                },
            )
            if ended:
                self.compactions.pop(session_id, None)
        elif kind in {"session.permission.requested", "session.question.requested"}:
            key = (session_id, data["id"])
            if key not in self.requests:
                task = asyncio.create_task(self.interact(session_id, kind, data))
                self.requests[key] = task
                task.add_done_callback(lambda _done: self.requests.pop(key, None))

    async def cancel_requests(self, session_id: str) -> None:
        tasks = [task for key, task in self.requests.items() if key[0] == session_id]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def interact(self, session_id: str, kind: str, data: dict[str, Any]) -> None:
        native = self.agent.native
        permission = kind == "session.permission.requested"
        path = native.path(
            session_id, ("/permission/" if permission else "/question/") + data["id"]
        )
        accepted = False
        answers: list[str] = []
        try:
            if permission:
                from yoke.acp.tools import ToolCallProjector

                call = data.get("toolCall")
                if call and isinstance(call.get("rawInput"), dict):
                    call = ToolCallProjector(self.roots.get(session_id)).update(
                        start=True,
                        data={
                            "tool_call_id": call["toolCallId"],
                            "tool_name": call["title"],
                            "tool_arguments": call["rawInput"],
                        },
                    )
                    call.pop("sessionUpdate")
                response = await self.agent.conn.request_permission(
                    session_id=session_id,
                    tool_call=call
                    or {"toolCallId": data["id"], "title": data["message"]},
                    options=[
                        {
                            "optionId": "allow",
                            "name": "Allow once",
                            "kind": "allow_once",
                        },
                        {"optionId": "deny", "name": "Deny", "kind": "reject_once"},
                    ],
                )
                outcome = response.outcome
                accepted = (
                    outcome.outcome == "selected" and outcome.option_id == "allow"
                )
            else:
                field: dict[str, Any] = {"type": "string", "title": data["question"]}
                options = data.get("options", [])
                if options:
                    field["enum"] = options
                if data.get("multiple"):
                    field = {
                        "type": "array",
                        "title": data["question"],
                        "items": field,
                        "minItems": 1,
                    }
                mode = s.ElicitationFormSessionMode.model_validate(
                    {
                        "sessionId": session_id,
                        "requestedSchema": {
                            "type": "object",
                            "properties": {"answer": field},
                            "required": ["answer"],
                        },
                    }
                )
                response = await self.agent.conn.create_elicitation(
                    message=data["question"], mode=mode
                )
                if response.action == "accept":
                    answer = response.content.get("answer")
                    answers = (
                        [answer]
                        if isinstance(answer, str)
                        else answer
                        if isinstance(answer, list)
                        else []
                    )
                    accepted = bool(answers)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a missing client form handler declines the request
            LOGGER.warning("ACP client could not answer an interactive request")
        finally:
            try:
                if permission:
                    await native.data(
                        "POST",
                        path + "/reply",
                        json={"reply": "allow" if accepted else "deny"},
                    )
                elif accepted:
                    await native.data(
                        "POST", path + "/reply", json={"answers": answers}
                    )
                else:
                    await native.data("POST", path + "/reject")
            except Exception:  # noqa: BLE001 - native cancellation may already have resolved it
                LOGGER.debug("Interactive request already resolved")

    async def watch(self) -> None:
        while self.sessions:
            queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1024)
            ready = asyncio.Event()
            stream = asyncio.create_task(self.agent.native.events(queue, ready))
            incoming: asyncio.Task[dict[str, Any]] | None = None
            try:
                await asyncio.wait_for(ready.wait(), 10)
                if stream.done():
                    await stream
                for session_id in list(self.sessions):
                    await self.refresh(session_id)
                while self.sessions:
                    incoming = asyncio.create_task(queue.get())
                    done, _ = await asyncio.wait(
                        (stream, incoming),
                        timeout=10,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if stream in done:
                        await stream
                    if incoming in done:
                        item = incoming.result()
                        session_id = item.get("sessionID")
                        if item["type"] == "catalog.updated":
                            for attached in list(self.sessions):
                                await self.refresh(attached, commands=True)
                        elif session_id in self.sessions:
                            await self.event(session_id, item["type"], item["data"])
                    else:
                        incoming.cancel()
                        await asyncio.gather(incoming, return_exceptions=True)
                        for session_id in list(self.sessions):
                            await self.refresh(session_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - reconnect and recover durable metadata
                LOGGER.debug("Session notification stream reconnecting")
                await asyncio.sleep(1)
            finally:
                stream.cancel()
                if incoming is not None:
                    incoming.cancel()
                await asyncio.gather(
                    stream, *([incoming] if incoming else []), return_exceptions=True
                )
