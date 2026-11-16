"""Queue translation for the daemon-backed interactive client."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from yoke.cli.backend.connection import DaemonRequestError
from yoke.cli.backend.models import PendingPrompt
from yoke.cli.backend.queue import LoadedPromptQueue, PromptQueueRevisionConflict
from yoke.cli.image_input import ImageAttachment


def load_queue(backend: Any) -> LoadedPromptQueue:
    data = backend.connection.session_request("GET", "/queue").get("data", {})
    prompts = []
    for item in data.get("items", []):
        prompt = item.get("prompt", {})
        prompts.append(
            PendingPrompt(
                prompt=prompt.get("text", ""),
                kind="steering" if item.get("delivery") == "steer" else "queued",
                id=item.get("id", uuid4().hex[:8]),
                created_at=item.get("createdAt", item.get("created_at", "")),
                paused=bool(item.get("paused", False)),
                attachments=list(prompt.get("attachments", [])),
                continuation=bool(prompt.get("continuation", False)),
            )
        )
    return LoadedPromptQueue(prompts, [], int(data.get("revision", 0)))


def commit_queue(
    backend: Any,
    prompts: list[PendingPrompt],
    pending_images: list[ImageAttachment],
    *,
    expected_revision: int | None,
) -> int:
    del pending_images
    loaded = load_queue(backend)
    expected = loaded.revision if expected_revision is None else expected_revision
    existing = {item.id: item for item in loaded.prompts}
    operations: list[dict[str, Any]] = []
    desired = {item.id for item in prompts}
    operations.extend(
        {"op": "remove", "id": item.id}
        for item in loaded.prompts
        if item.id not in desired
    )
    for item in prompts:
        payload = {
            "text": item.prompt,
            "attachments": item.attachments,
            "continuation": item.continuation,
        }
        if item.id in existing:
            operations.extend(
                [
                    {"op": "update", "id": item.id, "prompt": payload},
                    {
                        "op": "setDelivery",
                        "id": item.id,
                        "delivery": "steer" if item.kind == "steering" else "queue",
                    },
                    {"op": "setPaused", "id": item.id, "paused": item.paused},
                ]
            )
        else:
            backend.connection.session_request(
                "POST",
                "/prompt",
                json={
                    "id": item.id,
                    "clientID": backend.connection.owner,
                    "delivery": "steer" if item.kind == "steering" else "queue",
                    "resume": False,
                    "prompt": payload,
                },
            )
    if not operations:
        return expected
    try:
        data = backend.connection.session_request(
            "PATCH",
            "/queue",
            json={"expectedRevision": expected, "operations": operations},
        )["data"]
    except DaemonRequestError as exc:
        if exc.status != 409:
            raise
        details = exc.body.get("error", {}).get("details", {})
        actual = int(details.get("actualRevision", expected))
        raise PromptQueueRevisionConflict(
            expected_revision=expected, actual_revision=actual
        ) from exc
    return int(data.get("revision", expected))


def clear_queue(backend: Any, *, expected_revision: int | None) -> None:
    loaded = load_queue(backend)
    expected = loaded.revision if expected_revision is None else expected_revision
    if loaded.prompts:
        commit_queue(backend, [], [], expected_revision=expected)
