"""A turn this terminal started is never shown or edited as a queued prompt."""

from __future__ import annotations

import threading
from pathlib import Path

from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import Message
from yoke.ai.providers.base import Provider
from yoke.cli.backend.models import PendingPrompt

from .terminal_daemon_support import terminal as terminal


class HeldProvider(Provider):
    """Hold the first ordinary turn until released, so later inputs stay queued."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def complete(
        self, messages: list[Message], tools: list[dict[str, object]]
    ) -> Message:
        del tools
        if any(
            "Create a concise title" in (message.plain_text_content or "")
            for message in messages
        ):
            return Message.assistant("")
        self.entered.set()
        assert self.release.wait(10)
        return Message.assistant("done")


def test_started_turn_is_hidden_from_queue_and_kept_first(terminal, tmp_path: Path):
    provider = HeldProvider()
    terminal.factory = lambda record: RuntimeAgent(
        provider, [], tool_root=Path(record.root)
    )
    backend = terminal.backend("started", root=tmp_path)
    observer = terminal.backend("started", root=tmp_path)
    try:
        first = backend.submit("first", user_message=None)
        assert provider.entered.wait(10)
        # The running input is absent from the authoritative queue in both clients.
        assert backend.load_queue().prompts == []
        assert observer.load_queue().prompts == []
        loaded = backend.load_queue()
        backend.commit_queue(
            [PendingPrompt("second")], [], expected_revision=loaded.revision
        )
        raw = observer.load_queue()
        assert [item.prompt for item in raw.prompts] == ["second"]
        started_id = raw.prompts[0].id

        assert [item.prompt for item in backend.load_queue().prompts] == ["second"]

        # Queueing a follow-up must neither remove nor overtake the started turn.
        queued = PendingPrompt("third", kind="queued")
        loaded = backend.load_queue()
        backend.commit_queue(
            [*loaded.prompts, queued],
            loaded.pending_images,
            expected_revision=loaded.revision,
        )
        after = observer.load_queue().prompts
        assert [item.prompt for item in after] == ["second", "third"]
        assert after[0].id == started_id
        assert [item.prompt for item in backend.load_queue().prompts] == [
            "second",
            "third",
        ]
        snapshot = backend.connection.session_request("GET", "/input")["data"]
        assert snapshot["inputs"][0]["id"] == first.input_id
        assert snapshot["inputs"][0]["phase"] == "running"
    finally:
        provider.release.set()
