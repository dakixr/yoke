"""ACP agent backed by Yoke's process-wide native HTTP runtime."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from acp import RequestError
from acp import schema as s

from yoke._version import __version__
from yoke.acp.catalog import discover
from yoke.acp.native import NativeClient
from yoke.acp.native import fail
from yoke.acp.ownership import session_owner
from yoke.acp.prompting import history_content
from yoke.acp.turns import prompt as run_prompt

type AcpMcpServer = s.HttpMcpServer | s.SseMcpServer | s.AcpMcpServer | s.McpServerStdio


class YokeAcpAgent:
    """Translate ACP session operations into native Yoke HTTP operations."""

    def __init__(self, native: NativeClient) -> None:
        self.native = native
        self.catalogs: dict[str, dict[str, dict[str, Any]]] = {}
        self.catalog: dict[str, dict[str, Any]] = {}
        self.default_model: str | None = None
        self.sessions: dict[str, dict[str, Any]] = {}
        self.busy: set[str] = set()
        self.active: dict[str, dict[str, Any]] = {}
        self.conn: Any = None

    def on_connect(self, conn: Any) -> None:
        """Capture the ACP client connection for session notifications."""
        self.conn = conn

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: s.ClientCapabilities | None = None,
        client_info: s.Implementation | None = None,
        **_kwargs: Any,
    ) -> s.InitializeResponse:
        """Advertise Yoke and its authoritative ready model catalog."""
        del protocol_version, client_info
        client_meta = client_capabilities.field_meta if client_capabilities else None
        yoke_client_meta = (
            client_meta.get("yoke") if isinstance(client_meta, dict) else None
        )
        workspace = (
            yoke_client_meta.get("cwd") if isinstance(yoke_client_meta, dict) else None
        )
        try:
            async with asyncio.timeout(10):
                self.catalog, self.default_model = await discover(self.native)
                spec = await self.native.request("GET", "openapi.json")
                skills = (
                    await self.native.skills(workspace)
                    if isinstance(workspace, str)
                    else []
                )
        except TimeoutError:
            raise fail("Native initialization timed out") from None
        drain = "post" in spec.get("paths", {}).get(
            "/api/v1/session/{session_id}/drain", {}
        )
        return s.InitializeResponse.model_validate(
            {
                "protocolVersion": 1,
                "agentInfo": {"name": "yoke", "version": __version__},
                "authMethods": [{"id": "yoke", "name": "Yoke native daemon"}],
                "agentCapabilities": {
                    "loadSession": True,
                    "sessionCapabilities": {"resume": {}},
                    "promptCapabilities": {"image": True, "embeddedContext": True},
                },
                "_meta": {
                    "yoke": {
                        "protocol": 1,
                        "ready": drain,
                        "workerDrain": drain,
                        "models": list(self.catalog.values()),
                        "defaultModel": self.default_model,
                        "skills": [
                            {
                                "name": skill["name"],
                                "description": skill["description"],
                                "path": skill["sourcePath"],
                                "enabled": True,
                            }
                            for skill in skills
                        ],
                    }
                },
            }
        )

    async def authenticate(
        self, method_id: str, **_kwargs: Any
    ) -> s.AuthenticateResponse:
        """Verify that the configured native daemon accepts the bearer credential."""
        if method_id != "yoke":
            raise RequestError(-32602, "Unsupported authentication method")
        await self.native.data("GET", "session/active")
        return s.AuthenticateResponse()

    def options(self, session_id: str) -> list[dict[str, Any]]:
        """Return ACP model, reasoning, and mode controls for one session."""
        selection = self.sessions[session_id]
        catalog = self.catalogs[session_id]
        models = [
            {"value": model["id"], "name": model["name"]} for model in catalog.values()
        ]
        result: list[dict[str, Any]] = [
            {
                "id": "model",
                "name": "Model",
                "category": "model",
                "type": "select",
                "currentValue": selection["model"],
                "options": models,
            },
            {
                "id": "mode",
                "name": "Mode",
                "category": "mode",
                "type": "select",
                "currentValue": "full-access",
                "options": [{"value": "full-access", "name": "Full access"}],
            },
        ]
        efforts = catalog[selection["model"]]["reasoningEfforts"]
        if efforts:
            result.insert(
                1,
                {
                    "id": "reasoningEffort",
                    "name": "Reasoning effort",
                    "category": "thought_level",
                    "type": "select",
                    "currentValue": selection["effort"],
                    "options": [
                        {"value": effort, "name": effort} for effort in efforts
                    ],
                },
            )
        return result

    def remember(
        self,
        session_id: str,
        data: dict[str, Any],
        catalog: dict[str, dict[str, Any]],
        default: str,
    ) -> None:
        """Validate and cache native model selection for an ACP session."""
        selection = data.get("selection", {})
        model = ":".join(selection.get(key) or "" for key in ("provider", "model"))
        if model == ":":
            model = default
        if model not in catalog:
            raise fail("Native session model is not available in the ready catalog")
        effort = (
            selection.get("reasoningEffort") or catalog[model]["defaultReasoningEffort"]
        )
        if effort is not None and effort not in catalog[model]["reasoningEfforts"]:
            raise fail("Native session reasoning effort is unsupported by its model")
        self.catalogs[session_id] = catalog
        self.sessions[session_id] = {"model": model, "effort": effort}

    @asynccontextmanager
    async def exclusive(self, session_id: str):  # noqa: ANN202
        """Serialize operations within this peer and across local peers."""
        if session_id in self.busy:
            raise fail("Session already has an in-flight operation")
        self.busy.add(session_id)
        try:
            with session_owner(str(self.native.http.base_url), session_id):
                yield
        finally:
            self.busy.remove(session_id)

    def known(self, session_id: str) -> None:
        if session_id not in self.sessions:
            raise fail("Load or resume the native session first")

    @staticmethod
    def scope(
        cwd: str,
        mcp_servers: list[AcpMcpServer] | None,
        additional_directories: list[str] | None,
    ) -> str:
        if mcp_servers or additional_directories:
            raise RequestError(-32602, "MCP and additional directories are unsupported")
        if not Path(cwd).is_absolute():
            raise RequestError(-32602, "cwd must be absolute")
        return str(Path(cwd).resolve())

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[AcpMcpServer] | None = None,
        **_kwargs: Any,
    ) -> s.NewSessionResponse:
        cwd = self.scope(cwd, mcp_servers, additional_directories)
        catalog, default = await discover(self.native, cwd)
        provider, model = default.split(":", 1)
        data = await self.native.data(
            "POST",
            "session",
            json={
                "location": {"directory": cwd},
                "title": "T3 Code session",
                "selection": {
                    "provider": provider,
                    "model": model,
                    "reasoningEffort": catalog[default]["defaultReasoningEffort"],
                },
            },
        )
        session_id = data["id"]
        self.remember(session_id, data, catalog, default)
        return s.NewSessionResponse.model_validate(
            {"sessionId": session_id, "configOptions": self.options(session_id)}
        )

    async def restore(
        self,
        session_id: str,
        cwd: str,
        mcp_servers: list[AcpMcpServer] | None,
        additional_directories: list[str] | None,
    ) -> dict[str, Any]:
        cwd = self.scope(cwd, mcp_servers, additional_directories)
        async with self.exclusive(session_id):
            data = await self.native.data("GET", self.native.path(session_id))
            if str(Path(data["location"]["directory"]).resolve()) != cwd:
                raise fail(
                    "cwd mismatch: native session belongs to a different directory"
                )
            catalog, default = await discover(self.native, cwd)
            self.remember(session_id, data, catalog, default)
            return {"configOptions": self.options(session_id)}

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[AcpMcpServer] | None = None,
        additional_directories: list[str] | None = None,
        **_kwargs: Any,
    ) -> s.LoadSessionResponse:
        result = await self.restore(
            session_id, cwd, mcp_servers, additional_directories
        )
        async with self.exclusive(session_id):
            for message in await self.native.messages(session_id):
                if message["type"] not in ("user", "assistant"):
                    continue
                kind = (
                    "user_message_chunk"
                    if message["type"] == "user"
                    else "agent_message_chunk"
                )
                for block in message["content"]:
                    await self.update(
                        session_id,
                        {"sessionUpdate": kind, "content": history_content(block)},
                    )
        return s.LoadSessionResponse.model_validate(result)

    async def resume_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[AcpMcpServer] | None = None,
        **_kwargs: Any,
    ) -> s.ResumeSessionResponse:
        return s.ResumeSessionResponse.model_validate(
            await self.restore(session_id, cwd, mcp_servers, additional_directories)
        )

    async def list_sessions(
        self,
        cwd: str | None = None,
        cursor: str | None = None,
        **_kwargs: Any,
    ) -> s.ListSessionsResponse:
        del cwd, cursor
        raise RequestError(-32601, "ACP session listing is not supported")

    async def fork_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[AcpMcpServer] | None = None,
        **_kwargs: Any,
    ) -> s.ForkSessionResponse:
        del session_id, cwd, additional_directories, mcp_servers
        raise RequestError(-32601, "ACP session forking is not supported")

    async def close_session(
        self,
        session_id: str,
        **_kwargs: Any,
    ) -> s.CloseSessionResponse:
        work = self.active.get(session_id)
        if work is not None:
            work["cancel"] = True
            work["wake"].set()
            await work["done"].wait()
        self.sessions.pop(session_id, None)
        self.catalogs.pop(session_id, None)
        return s.CloseSessionResponse()

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        del params
        raise RequestError(-32601, f"Unsupported ACP extension method: {method}")

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        del method, params

    async def set_config_option(
        self,
        config_id: str,
        session_id: str,
        value: str | bool,
        **_kwargs: Any,
    ) -> s.SetSessionConfigOptionResponse:
        self.known(session_id)
        async with self.exclusive(session_id):
            if config_id == "mode":
                if value != "full-access":
                    raise RequestError(-32602, "Only full-access mode is supported")
            elif config_id in ("model", "reasoningEffort"):
                if not isinstance(value, str):
                    raise RequestError(-32602, "Unsupported config value")
                catalog = self.catalogs[session_id]
                selection = dict(self.sessions[session_id])
                if config_id == "model":
                    if value not in catalog:
                        raise RequestError(-32602, "Unsupported model")
                    selection = {
                        "model": value,
                        "effort": catalog[value]["defaultReasoningEffort"],
                    }
                else:
                    if value not in catalog[selection["model"]]["reasoningEfforts"]:
                        raise RequestError(-32602, "Unsupported reasoning effort")
                    selection["effort"] = value
                provider, model = selection["model"].split(":", 1)
                await self.native.assert_idle(session_id)
                await self.native.data(
                    "POST",
                    self.native.path(session_id, "/selection"),
                    json={
                        "provider": provider,
                        "model": model,
                        "reasoningEffort": selection["effort"],
                    },
                )
                self.sessions[session_id] = selection
            else:
                raise RequestError(-32602, "Unsupported config option or model")
            return s.SetSessionConfigOptionResponse.model_validate(
                {"configOptions": self.options(session_id)}
            )

    async def set_session_mode(
        self, session_id: str, mode_id: str, **_kwargs: Any
    ) -> s.SetSessionModeResponse:
        await self.set_config_option("mode", session_id, mode_id)
        return s.SetSessionModeResponse()

    async def update(self, session_id: str, update: dict[str, Any]) -> None:
        if self.conn is None:
            raise fail("ACP client connection is unavailable")
        await self.conn.session_update(session_id=session_id, update=update)

    async def prompt(
        self,
        session_id: str,
        prompt: list[Any],
        **kwargs: Any,
    ) -> s.PromptResponse:
        return await run_prompt(self, session_id, prompt, kwargs)

    async def cancel(self, session_id: str, **_kwargs: Any) -> None:
        if work := self.active.get(session_id):
            work["cancel"] = True
            work["wake"].set()
            await work["done"].wait()

    async def close(self) -> None:
        """Cancel owned prompt handlers and close the native client."""
        work = list(self.active.values())
        for item in work:
            task = item.get("task")
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *(item["task"] for item in work if item.get("task") is not None),
            return_exceptions=True,
        )
        await self.native.close()
