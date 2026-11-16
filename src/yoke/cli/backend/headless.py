"""One headless turn using daemon admission, events, and transcript reads."""

from __future__ import annotations

import mimetypes
import httpx
from pathlib import Path
from typing import Any
from uuid import uuid4

from yoke.agent.models import (
    Message,
    MessageImageURL,
    MessageImageURLContentPart,
    MessageTextContentPart,
    ToolCall,
    ToolFunction,
)
from yoke.cli.backend.connection import SessionConnection
from yoke.cli.backend.events import agent_event
from yoke.cli.render import (
    OutputStream,
    StatusIndicator,
    build_console,
    print_agent_output,
    print_error,
)


def upload_images(
    connection: SessionConnection, paths: tuple[Path, ...]
) -> list[dict[str, str]]:
    """Upload terminal-owned images; credentials and local paths stay local."""
    attachments = []
    for path in paths:
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with path.open("rb") as image:
            uploaded = connection.request(
                "POST",
                "upload",
                params={"sessionID": connection.session_id},
                files={"file": (path.name, image, mime)},
            )["data"]
        attachments.append(
            {
                "type": "file",
                "uri": uploaded["uri"],
                "name": path.name,
                "mime": mime,
                "imageProjection": "cli",
            }
        )
    return attachments


def projected_message(value: dict[str, Any]) -> Message | None:
    """Restore presentation values from a public transcript, never provider state."""
    kind = value["type"]
    if kind == "control":
        return None
    if kind == "tool":
        return Message(
            role="tool", content=value.get("result"), tool_call_id=value.get("callID")
        )
    parts = []
    for item in value.get("content", []):
        if item["type"] == "text":
            parts.append(MessageTextContentPart(text=item["text"]))
        elif item.get("uri"):
            parts.append(
                MessageImageURLContentPart(
                    image_url=MessageImageURL(url=item["uri"]), label=item["name"]
                )
            )
    calls = [
        ToolCall(
            id=item["id"],
            function=ToolFunction(name=item["name"], arguments=item["arguments"]),
        )
        for item in value.get("toolCalls", [])
    ]
    return Message(role=kind, content=parts, phase=value.get("phase"), tool_calls=calls)


def input_output(connection: SessionConnection, input_id: str) -> str:
    """Read the terminal answer belonging to this input even if others follow it."""
    cursor = None
    found = False
    output = ""
    while True:
        params = {"order": "asc", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        page = connection.session_request("GET", "/message", params=params)
        for value in page["data"]:
            if value["type"] == "user":
                if found:
                    return output
                found = value.get("inputID") == input_id
            elif found and value["type"] == "assistant" and not value.get("toolCalls"):
                message = projected_message(value)
                if message is not None:
                    output = message.final_text_content() or ""
        cursor = page["cursor"]["next"]
        if not cursor:
            return output


def run_headless_prompt(
    connection: SessionConnection,
    prompt: str,
    *,
    image_paths: tuple[Path, ...] = (),
    stdout: OutputStream,
    stderr: OutputStream,
) -> int:
    """Render this input's events and final answer with existing renderers."""
    connection.start()
    attachments = upload_images(connection, image_paths)
    stream, unsubscribe = connection.events.subscribe()
    input_id = f"inp_{uuid4().hex}"
    try:
        admission = {
            "id": input_id,
            "clientID": connection.owner,
            "delivery": "queue",
            "prompt": {"text": prompt, "attachments": attachments},
        }
        try:
            connection.session_request("POST", "/prompt", json=admission)
        except httpx.HTTPError:
            # The input ID makes an uncertain admission safe to replay after
            # transport recovery; a different mutation must not use this rule.
            connection.start()
            connection.session_request("POST", "/prompt", json=admission)
        with StatusIndicator(stderr, animate=False, headless=True) as indicator:
            for event in stream:
                payload = event["payload"]
                if payload.get("inputID") != input_id:
                    continue
                if event["type"] == "session.runtime.failed":
                    print_error(
                        build_console(stderr),
                        str(payload.get("error", "Agent execution failed.")),
                    )
                    return 1
                if event["type"] == "session.prompt.settled":
                    outcome = payload.get("outcome")
                    if outcome == "completed":
                        print_agent_output(
                            build_console(stdout), input_output(connection, input_id)
                        )
                        return 0
                    if outcome in {"stopped", "recovered"}:
                        print_error(build_console(stderr), "Stopped current turn.")
                        return 1
                indicator.handle_event(*agent_event(event))
        print_error(
            build_console(stderr),
            "Daemon event stream closed before the turn finished.",
        )
        return 1
    except KeyboardInterrupt:
        connection.session_request("POST", "/interrupt")
        return 130
    finally:
        unsubscribe()
