"""Capabilities supplied by an interactive command's front end."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from yoke.agent.models import Message
from yoke.cli.image_input import ImageAttachment
from yoke.cli.interactive.common import PendingPrompt
from yoke.cli.render.base import Console
from yoke.cli.backend.daemon import DaemonActiveSession
from yoke.cli.backend.daemon import DaemonSessionBackend


class CommandHost(Protocol):
    """Explicit front-end capabilities and command notifications."""

    basic_mode: bool
    supports_tree: bool

    def update_context_usage(self, payload: dict[str, object]) -> None: ...
    def restore_editor(self, text: str) -> None: ...
    def submit_prompt(self, prompt: str) -> None: ...
    def queue_changed(self) -> None: ...
    def replace_queue(
        self, prompts: list[PendingPrompt]
    ) -> tuple[bool, str | None]: ...
    def attach_image(self, image: ImageAttachment) -> bool: ...
    def replay_messages(self, messages: list[Message]) -> bool: ...
    def inspect_processes(self) -> bool: ...
    def start_compaction(self) -> bool: ...


@dataclass(slots=True)
class CommandCallbacks:
    """Adapt front-end callbacks to the command host contract."""

    basic_mode: bool = False
    supports_tree: bool = False
    on_context_usage: Callable[[dict[str, object]], None] | None = None
    on_editor_text: Callable[[str], None] | None = None
    on_submit_prompt: Callable[[str], None] | None = None
    on_queue_changed: Callable[[], None] | None = None
    on_queue_replace: Callable[[list[PendingPrompt]], str | None] | None = None
    on_image_attached: Callable[[ImageAttachment], None] | None = None
    on_replay_messages: Callable[[list[Message]], None] | None = None
    on_process_inspector: Callable[[], None] | None = None
    start_compaction: Callable[[], bool] = lambda: False

    def update_context_usage(self, payload: dict[str, object]) -> None:
        if self.on_context_usage is not None:
            self.on_context_usage(payload)

    def restore_editor(self, text: str) -> None:
        if self.on_editor_text is not None:
            self.on_editor_text(text)

    def submit_prompt(self, prompt: str) -> None:
        if self.on_submit_prompt is not None:
            self.on_submit_prompt(prompt)

    def queue_changed(self) -> None:
        if self.on_queue_changed is not None:
            self.on_queue_changed()

    def replace_queue(self, prompts: list[PendingPrompt]) -> tuple[bool, str | None]:
        if self.on_queue_replace is None:
            return False, None
        return True, self.on_queue_replace(prompts)

    def attach_image(self, image: ImageAttachment) -> bool:
        if self.on_image_attached is None:
            return False
        self.on_image_attached(image)
        return True

    def replay_messages(self, messages: list[Message]) -> bool:
        if self.on_replay_messages is None:
            return False
        self.on_replay_messages(messages)
        return True

    def inspect_processes(self) -> bool:
        if self.on_process_inspector is None:
            return False
        self.on_process_inspector()
        return True


@dataclass(slots=True)
class CommandContext:
    """State owned by one command invocation."""

    backend: DaemonSessionBackend
    active_session: DaemonActiveSession
    messages: list[Message]
    console: Console
    host: CommandHost
    pending_images: list[ImageAttachment] | None = None
    pending_prompts: list[PendingPrompt] | None = None
