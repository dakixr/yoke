"""Daemon-only queue assertions and terminal session fixtures."""

from __future__ import annotations


import pytest

from yoke.cli.backend.daemon.presentation import DaemonActiveSession
from yoke.cli.backend.models import PendingPrompt
from yoke.cli.backend.values import (
    PromptQueueRevisionConflict as PromptQueueRevisionConflict,
)
from yoke.cli.image_input import ImageAttachment
from yoke.session import SessionStore

from .terminal_daemon_support import TerminalDaemon, session_for, terminal as terminal


@pytest.fixture
def active_session_for(terminal: TerminalDaemon):
    return lambda root: session_for(terminal.backend("saved", root=root))


def remote(active: DaemonActiveSession) -> DaemonActiveSession:
    return active


def store_for(active: DaemonActiveSession) -> SessionStore:
    return SessionStore(database_path=remote(active).database_path)


def load_prompt_queue_state(active: DaemonActiveSession):
    return remote(active).backend.load_queue()


def load_prompt_queue(active: DaemonActiveSession):
    loaded = load_prompt_queue_state(active)
    return loaded.prompts, loaded.pending_images


def commit_prompt_queue(
    active: DaemonActiveSession,
    prompts: list[PendingPrompt],
    pending_images: list[ImageAttachment] | None = None,
    *,
    expected_revision: int | None = None,
) -> int:
    return remote(active).backend.commit_queue(
        prompts, pending_images or [], expected_revision=expected_revision
    )


def persist_prompt_queue(
    active: DaemonActiveSession,
    prompts: list[PendingPrompt],
    pending_images: list[ImageAttachment] | None = None,
    *,
    expected_revision: int | None = None,
) -> None:
    commit_prompt_queue(
        active, prompts, pending_images, expected_revision=expected_revision
    )


def clear_prompt_queue(
    active: DaemonActiveSession, *, expected_revision: int | None = None
) -> None:
    commit_prompt_queue(active, [], [], expected_revision=expected_revision)


def wake(active: DaemonActiveSession) -> None:
    backend = remote(active).backend
    loaded = backend.load_queue()
    if loaded.prompts:
        item = loaded.prompts[0]
        backend.connection.session_request(
            "POST",
            "/prompt",
            json={
                "id": item.id,
                "clientID": backend.connection.owner,
                "delivery": "steer" if item.kind == "steering" else "queue",
                "prompt": {
                    "text": item.prompt,
                    "continuation": item.continuation,
                    "attachments": item.attachments,
                },
            },
        )
    backend.connection.session_request("POST", "/wait", params={"timeoutMs": 5000})
