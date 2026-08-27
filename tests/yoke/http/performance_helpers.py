"""Shared fixtures for HTTP performance regression tests."""

from yoke.agent.models import Message
from yoke.http.models.session import ProjectedMessage


TOKEN = "perf-test"


def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def projected_text(item: ProjectedMessage) -> str:
    if item.type == "user":
        content = item.content
    elif item.type == "assistant":
        content = item.content
    else:
        return ""
    return "\n".join(part.text for part in content if part.type == "text")


def messages(count: int, *, payload: int = 32) -> list[Message]:
    return [
        Message.user(f"{index:05d} " + "x" * payload)
        if index % 2 == 0
        else Message.assistant(f"{index:05d} " + "x" * payload)
        for index in range(count)
    ]
