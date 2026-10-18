"""ACP session lifecycle and ephemeral client activation."""

from __future__ import annotations

from pathlib import Path
from typing import Any, TYPE_CHECKING
from uuid import uuid4

from acp import RequestError
from acp import schema as s

from yoke.acp.native import fail
from yoke.acp.prompting import replay_history

if TYPE_CHECKING:
    from yoke.acp.bridge import YokeAcpAgent

MODES = {
    "full-access": "Full access",
    "approval-required": "Ask before changes",
    "auto-accept-edits": "Accept file edits",
    "plan": "Plan",
}


class SessionLifecycle:
    def __init__(self, agent: YokeAcpAgent) -> None:
        self.agent = agent
        self.owner = uuid4().hex
        self.enabled = False
        self.configs: dict[str, dict[str, Any]] = {}

    @staticmethod
    def scope(cwd: str, additional_directories: list[str] | None) -> str:
        if not Path(cwd).is_absolute():
            raise RequestError(-32602, "cwd must be absolute")
        if any(not Path(item).is_absolute() for item in additional_directories or []):
            raise RequestError(-32602, "Additional directories must be absolute")
        return str(Path(cwd).resolve())

    @staticmethod
    def servers(mcp_servers: list[Any] | None) -> list[dict[str, Any]]:
        servers: list[dict[str, Any]] = []
        for server in mcp_servers or []:
            data = (
                server.model_dump(by_alias=True, exclude_none=True)
                if hasattr(server, "model_dump")
                else server
            )
            kind = data.get("type", "stdio")
            if kind not in {"stdio", "http"}:
                raise RequestError(
                    -32602, "Supported client MCP transports are stdio and HTTP"
                )
            entry: dict[str, Any] = {
                "name": data["name"],
                "transport": "stdio" if kind == "stdio" else "streamable-http",
            }
            if kind == "stdio":
                entry.update(
                    command=data["command"],
                    args=data.get("args", []),
                    env={item["name"]: item["value"] for item in data.get("env", [])},
                )
            else:
                entry.update(
                    url=data["url"],
                    headers={
                        item["name"]: item["value"] for item in data.get("headers", [])
                    },
                )
            servers.append(entry)
        if len({server["name"] for server in servers}) != len(servers):
            raise RequestError(-32602, "MCP server names must be unique")
        return servers

    async def configure(
        self,
        session_id: str,
        cwd: str,
        mcp_servers: list[Any] | None,
        additional_directories: list[str] | None,
    ) -> None:
        if not self.enabled:
            if mcp_servers or additional_directories:
                raise fail(
                    "Upgrade the Yoke daemon to support client MCP servers and additional directories"
                )
            return
        servers = self.servers(mcp_servers)
        config = {
            "owner": self.owner,
            "mode": self.configs.get(session_id, {}).get("mode", "approval-required"),
            "mcpServers": servers,
            "additionalDirectories": additional_directories or [],
        }
        await self.agent.native.data(
            "PUT", self.agent.native.path(session_id, "/client"), json=config
        )
        self.configs[session_id] = config

    async def activate(self, session_id: str) -> None:
        agent = self.agent
        await agent.native.agent_monitor.attach(session_id, agent.update)
        if self.enabled and agent.conn is not None:
            await agent.notifications.attach(session_id)

    async def new(
        self,
        cwd: str,
        mcp_servers: list[Any] | None,
        additional_directories: list[str] | None,
    ) -> s.NewSessionResponse:
        agent = self.agent
        cwd = self.scope(cwd, additional_directories)
        self.servers(mcp_servers)
        catalog, default = await agent.discover(cwd)
        provider, model = default.split(":", 1)
        data = await agent.native.data(
            "POST",
            "session",
            json={
                "location": {"directory": cwd},
                "selection": {
                    "provider": provider,
                    "model": model,
                    "reasoningEffort": catalog[default]["defaultReasoningEffort"],
                },
            },
        )
        session_id = data["id"]
        await self.configure(session_id, cwd, mcp_servers, additional_directories)
        agent.remember(session_id, data, catalog, default)
        await self.activate(session_id)
        return s.NewSessionResponse.model_validate(
            {"sessionId": session_id, **self.state(session_id)}
        )

    async def restore(
        self,
        session_id: str,
        cwd: str,
        mcp_servers: list[Any] | None,
        additional_directories: list[str] | None,
        *,
        replay: bool,
    ) -> dict[str, Any]:
        agent = self.agent
        cwd = self.scope(cwd, additional_directories)
        async with agent.exclusive(session_id):
            data = await agent.native.data("GET", agent.native.path(session_id))
            if str(Path(data["location"]["directory"]).resolve()) != cwd:
                raise fail(
                    "cwd mismatch: native session belongs to a different directory"
                )
            catalog, default = await agent.discover(cwd)
            if self.enabled:
                await agent.native.drained(session_id)
            await self.configure(session_id, cwd, mcp_servers, additional_directories)
            agent.remember(session_id, data, catalog, default)
            if replay:
                await replay_history(
                    session_id, await agent.native.messages(session_id), agent.update
                )
            await self.activate(session_id)
            return self.state(session_id)

    def state(self, session_id: str) -> dict[str, Any]:
        result: dict[str, Any] = {"configOptions": self.agent.options(session_id)}
        if self.enabled:
            result["modes"] = {
                "currentModeId": self.configs.get(session_id, {}).get(
                    "mode", "full-access"
                ),
                "availableModes": [
                    {"id": key, "name": value} for key, value in MODES.items()
                ],
            }
        return result

    async def mode(self, session_id: str, value: str | bool) -> None:
        if value not in MODES or not isinstance(value, str):
            raise RequestError(-32602, "Unsupported session mode")
        if not self.enabled:
            if value != "full-access":
                raise RequestError(
                    -32602,
                    "Only full-access mode is supported by this daemon; upgrade for interactive modes",
                )
            return
        config = {**self.configs[session_id], "mode": value}
        await self.agent.native.data(
            "PUT", self.agent.native.path(session_id, "/client"), json=config
        )
        self.configs[session_id] = config
        await self.agent.update(
            session_id, {"sessionUpdate": "current_mode_update", "currentModeId": value}
        )

    async def list_sessions(
        self, cwd: str | None, cursor: str | None
    ) -> s.ListSessionsResponse:
        params: dict[str, Any] = {"limit": 100, "archived": False}
        if cwd is not None:
            params["directory"] = self.scope(cwd, None)
        if cursor is not None:
            params["cursor"] = cursor
        result = await self.agent.native.request("GET", "session", params=params)
        return s.ListSessionsResponse.model_validate(
            {
                "sessions": [
                    {
                        "sessionId": item["id"],
                        "cwd": item["location"]["directory"],
                        "title": item.get("title"),
                        "updatedAt": item.get("time", {}).get("updated"),
                    }
                    for item in result["data"]
                ],
                "nextCursor": result.get("cursor", {}).get("next"),
            }
        )

    async def fork(
        self,
        session_id: str,
        cwd: str,
        mcp_servers: list[Any] | None,
        additional_directories: list[str] | None,
    ) -> s.ForkSessionResponse:
        agent = self.agent
        cwd = self.scope(cwd, additional_directories)
        self.servers(mcp_servers)
        async with agent.exclusive(session_id):
            await agent.native.assert_idle(session_id)
            await agent.native.drained(session_id)
            data = await agent.native.data(
                "POST",
                agent.native.path(session_id, "/fork"),
                json={"location": {"directory": cwd}},
            )
        new_id = data["id"]
        catalog, default = await agent.discover(cwd)
        await self.configure(new_id, cwd, mcp_servers, additional_directories)
        agent.remember(new_id, data, catalog, default)
        await self.activate(new_id)
        return s.ForkSessionResponse.model_validate(
            {"sessionId": new_id, **self.state(new_id)}
        )

    async def close(self, session_id: str) -> None:
        agent = self.agent
        work = agent.active.get(session_id)
        if work is not None:
            work["cancel"] = True
            work["wake"].set()
            await work["done"].wait()
        await agent.notifications.detach(session_id)
        await agent.native.agent_monitor.detach(session_id)
        if session_id in self.configs:
            await agent.native.data(
                "DELETE",
                agent.native.path(session_id, "/client"),
                params={"owner": self.owner},
            )
            self.configs.pop(session_id, None)
        agent.sessions.pop(session_id, None)
        agent.catalogs.pop(session_id, None)
