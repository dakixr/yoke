"""ACP agent backed by Yoke's process-wide native HTTP runtime."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from acp import RequestError
from acp import schema as s

from yoke._version import __version__
from yoke.acp.catalog import discover
from yoke.acp.native import NativeClient
from yoke.acp.native import fail
from yoke.acp.ownership import session_owner
from yoke.acp.turns import prompt as run_prompt
from yoke.acp.sessions import SessionLifecycle, MODES
from yoke.acp.notifications import SessionNotifications

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
        self.lifecycle = SessionLifecycle(self)
        self.notifications = SessionNotifications(self)

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
        self.lifecycle.enabled = "put" in spec.get("paths", {}).get(
            "/api/v1/session/{session_id}/client", {}
        )
        drain = "post" in spec.get("paths", {}).get(
            "/api/v1/session/{session_id}/drain", {}
        )
        return s.InitializeResponse.model_validate(
            {
                "protocolVersion": 1,
                "agentInfo": {"name": "yoke", "version": __version__},
                "authMethods": [],
                "agentCapabilities": {
                    "loadSession": True,
                    "sessionCapabilities": {
                        "resume": {},
                        **(
                            {
                                "close": {},
                                "list": {},
                                "fork": {},
                                "additionalDirectories": {},
                            }
                            if self.lifecycle.enabled
                            else {}
                        ),
                    },
                    "mcpCapabilities": {"http": self.lifecycle.enabled},
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
                "currentValue": self.lifecycle.configs.get(session_id, {}).get(
                    "mode", "full-access"
                ),
                "options": [
                    {"value": key, "name": value}
                    for key, value in (
                        MODES.items()
                        if self.lifecycle.enabled
                        else {"full-access": "Full access"}.items()
                    )
                ],
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

    async def discover(self, cwd: str) -> tuple[dict[str, dict[str, Any]], str]:
        return await discover(self.native, cwd)

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[AcpMcpServer] | None = None,
        **_kwargs: Any,
    ) -> s.NewSessionResponse:
        return await self.lifecycle.new(cwd, mcp_servers, additional_directories)

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[AcpMcpServer] | None = None,
        additional_directories: list[str] | None = None,
        **_kwargs: Any,
    ) -> s.LoadSessionResponse:
        return s.LoadSessionResponse.model_validate(
            await self.lifecycle.restore(
                session_id, cwd, mcp_servers, additional_directories, replay=True
            )
        )

    async def resume_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[AcpMcpServer] | None = None,
        **_kwargs: Any,
    ) -> s.ResumeSessionResponse:
        return s.ResumeSessionResponse.model_validate(
            await self.lifecycle.restore(
                session_id, cwd, mcp_servers, additional_directories, replay=False
            )
        )

    async def list_sessions(
        self, cwd: str | None = None, cursor: str | None = None, **_kwargs: Any
    ) -> s.ListSessionsResponse:
        return await self.lifecycle.list_sessions(cwd, cursor)

    async def fork_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[AcpMcpServer] | None = None,
        **_kwargs: Any,
    ) -> s.ForkSessionResponse:
        return await self.lifecycle.fork(
            session_id, cwd, mcp_servers, additional_directories
        )

    async def close_session(
        self, session_id: str, **_kwargs: Any
    ) -> s.CloseSessionResponse:
        await self.lifecycle.close(session_id)
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
            return await self.set_config_option_unlocked(config_id, session_id, value)

    async def set_config_option_unlocked(
        self, config_id: str, session_id: str, value: str | bool
    ) -> s.SetSessionConfigOptionResponse:
        if config_id == "mode":
            await self.lifecycle.mode(session_id, value)
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
        if self.lifecycle.enabled:
            await self.update(
                session_id,
                {
                    "sessionUpdate": "config_option_update",
                    "configOptions": self.options(session_id),
                },
            )
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
        self.known(session_id)
        if self.lifecycle.enabled:
            from yoke.acp.commands import run_command

            response = await run_command(self, session_id, prompt)
            if response is not None:
                return response
        return await run_prompt(self, session_id, prompt, kwargs)

    async def cancel(self, session_id: str, **_kwargs: Any) -> None:
        if work := self.active.get(session_id):
            work["cancel"] = True
            work["wake"].set()
            await self.notifications.cancel_requests(session_id)
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
        await self.notifications.close()
        for session_id in list(self.lifecycle.configs):
            try:
                await self.lifecycle.close(session_id)
            except RequestError:
                pass
        await self.native.close()
