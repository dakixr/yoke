"""Attach client tools without changing ordinary CLI/SDK tool registration."""

from __future__ import annotations

from yoke.agent.loop.agent import RuntimeAgent
from yoke.agent.models import Message
from yoke.agent.tools.client import client_tools
from yoke.agent.tools.context import (
    ToolRegistrationContext,
    ToolRegistrationResult,
    normalize_tool_registration,
)
from yoke.agent.tools.mcp import LazyMcpManager, register_mcp_tools


def install_client_tools(agent: RuntimeAgent) -> None:
    original = agent._tool_factory
    if getattr(original, "_yoke_client_factory", False):
        return
    initial_tools = tuple(agent.tools.values())

    def register(context: ToolRegistrationContext) -> ToolRegistrationResult:
        base = normalize_tool_registration(
            original(context) if original else initial_tools
        )
        client = getattr(context.provider, "_yoke_client_session", None)
        if client is None:
            return base
        tools = list(base.tools)
        names = {tool.name for tool in tools}
        extras = list(client_tools(client))
        policy = getattr(context.provider, "_yoke_mcp_session_policy", None)
        if policy is not None and policy.injected_servers:
            extras.extend(
                register_mcp_tools(
                    LazyMcpManager(
                        root=context.root, home=context.home, session_policy=policy
                    )
                )
            )
        tools.extend(tool for tool in extras if tool.name not in names)
        instructions = list(base.system_messages)
        if client.config.mode == "plan":
            instructions.append(
                Message.system(
                    "The client selected plan mode. Inspect files and develop a plan. File mutations, command execution, and unclassified MCP calls are blocked until the client switches mode."
                )
            )
        if client.config.additional_directories:
            instructions.append(
                Message.system(
                    "Additional workspace directories authorized by the client:\n"
                    + "\n".join(client.config.additional_directories)
                )
            )
        return ToolRegistrationResult(tools=tools, system_messages=instructions)

    setattr(register, "_yoke_client_factory", True)
    agent._tool_factory = register
