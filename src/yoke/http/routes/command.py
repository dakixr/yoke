"""Static command-palette metadata."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi import Depends

from yoke.http.auth import require_auth
from yoke.http.models.command import CommandInfo
from yoke.http.models.command import CommandListResponse


router = APIRouter(dependencies=[Depends(require_auth)])


COMMANDS = (
    CommandInfo(name="compact", description="Compact older conversation context.", action="session.compact"),
    CommandInfo(name="shortcuts", description="Show available interactive commands.", action="command.list"),
    CommandInfo(name="new", description="Create a new session.", action="session.create"),
    CommandInfo(name="pin", description="Pin or unpin the current session.", action="session.patch"),
    CommandInfo(name="info", description="Inspect current session metadata.", action="session.get"),
    CommandInfo(name="fork", description="Fork the current session.", action="session.fork"),
    CommandInfo(name="title", description="Set the current session title.", usage="/title <title>", action="session.patch"),
    CommandInfo(name="regenerate-title", description="Generate a new title from the current conversation.", action="session.title.regenerate"),
    CommandInfo(name="tree", description="Inspect or navigate the session tree.", action="session.tree"),
    CommandInfo(name="model", description="Select provider, model, and reasoning effort.", action="session.selection"),
    CommandInfo(name="tools", description="Inspect or change session tool enablement.", action="session.tool"),
    CommandInfo(name="mcp", description="Inspect or change MCP session policy.", action="session.mcp"),
    CommandInfo(name="queue", description="Inspect or edit pending prompts.", action="session.queue"),
    CommandInfo(name="ps", description="Inspect managed command processes.", action="process.list"),
    CommandInfo(name="image", description="Attach an image to a prompt.", action="upload.create"),
    CommandInfo(name="skill", description="Activate a discovered skill.", usage="/skill <name> [prompt]", action="session.skill"),
)


@router.get("/command", response_model=CommandListResponse, operation_id="listCommands")
def list_commands() -> CommandListResponse:
    return CommandListResponse(data=list(COMMANDS))
