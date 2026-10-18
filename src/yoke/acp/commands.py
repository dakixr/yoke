"""Executable ACP commands advertised alongside workspace skills."""

from __future__ import annotations

import json
from typing import Any, TYPE_CHECKING
from urllib.parse import quote

from acp import schema as s

if TYPE_CHECKING:
    from yoke.acp.bridge import YokeAcpAgent

COMMANDS = {
    "compact": ("Compact older conversation context", None),
    "info": ("Show session metadata", None),
    "title": ("Set the session title", "New title"),
    "model": (
        "Select a provider:model and optional reasoning effort",
        "provider:model [effort]",
    ),
    "skill": ("Activate a workspace skill", "Skill name"),
    "ps": ("Show managed processes", None),
    "agents": ("Show Yoke SDK agents", None),
    "mcp": ("Show connected MCP servers", None),
    "shortcuts": ("Show ACP commands", None),
}


def available_commands(skills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "description": description,
            **({"input": {"hint": hint}} if hint else {}),
        }
        for name, (description, hint) in COMMANDS.items()
    ] + [
        {"name": "$" + skill["name"], "description": skill["description"]}
        for skill in skills
    ]


async def run_command(
    agent: YokeAcpAgent, session_id: str, blocks: list[Any]
) -> s.PromptResponse | None:
    if len(blocks) != 1:
        return None
    block = blocks[0].model_dump() if hasattr(blocks[0], "model_dump") else blocks[0]
    if block.get("type") != "text":
        return None
    text = block.get("text", "").strip()
    command, _, argument = text.partition(" ")
    if not command.startswith("/") or command[1:] not in COMMANDS:
        return None
    command = command[1:]
    native = agent.native
    async with agent.exclusive(session_id):
        if command == "compact":
            await native.data(
                "POST", native.path(session_id, "/compact"), json={"reason": "manual"}
            )
            status = await native.data(
                "POST", native.path(session_id, "/wait?timeoutMs=30000")
            )
            result: Any = (
                "Compaction failed: " + str(status.get("lastError"))
                if status["state"] == "error"
                else "Compaction is still running."
                if status["state"] != "idle"
                else "Compaction finished. Older context was summarized if needed."
            )
        elif command == "title":
            result = await native.data(
                "PATCH", native.path(session_id), json={"title": argument}
            )
        elif command == "model":
            selection = argument.split()
            if selection:
                await agent.set_config_option_unlocked(
                    "model", session_id, selection[0]
                )
                if len(selection) > 1:
                    await agent.set_config_option_unlocked(
                        "reasoningEffort", session_id, selection[1]
                    )
            result = agent.options(session_id)
        elif command == "skill":
            result = await native.data(
                "POST",
                native.path(
                    session_id, "/skill/" + quote(argument, safe="") + "/activate"
                ),
                json={},
            )
        elif command == "shortcuts":
            result = "\n".join(
                "/" + name + " - " + value[0] for name, value in COMMANDS.items()
            )
        else:
            suffix = {
                "info": "",
                "ps": "/process",
                "agents": "/agent-run",
                "mcp": "/mcp",
            }[command]
            if command in {"agents", "ps"}:
                result = await native.data(
                    "GET",
                    "agent-run" if command == "agents" else "process",
                    params={"sessionID": session_id},
                )
            else:
                result = await native.data("GET", native.path(session_id, suffix))
        await agent.update(
            session_id,
            {
                "sessionUpdate": "agent_message_chunk",
                "content": {
                    "type": "text",
                    "text": result
                    if isinstance(result, str)
                    else json.dumps(result, indent=2),
                },
            },
        )
        if agent.lifecycle.enabled:
            await agent.notifications.refresh(session_id)
    return s.PromptResponse(stop_reason="end_turn")
