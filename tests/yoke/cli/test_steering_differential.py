"""Gate daemon steering against the established CLI interrupt then queue path."""

import json
from pathlib import Path
from types import SimpleNamespace
from threading import Event
from collections.abc import Callable

from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import Message
from yoke.ai.providers.base import ProviderCancelledError

from tests.yoke.loop.test_runtime_request_differential import RecordingProvider
from .terminal_daemon_support import terminal as terminal


class SteeringProvider(RecordingProvider):
    def __init__(self):
        super().__init__(
            [Message.assistant("discarded"), Message.assistant("corrected")]
        )
        self.entered = Event()

    def complete_with_cancel(
        self, messages, tools, *, cancel_requested: Callable[[], bool]
    ):
        response = self.complete(messages, tools)
        if len(self.records) == 1:
            self.entered.set()
            # Wait only until the runtime has fenced this worker.
            while not cancel_requested():
                Event().wait(0.001)
            raise ProviderCancelledError("stopped")
        return response


def test_daemon_steer_matches_interrupt_then_queue_provider_bytes_and_tree(
    terminal, tmp_path, monkeypatch
):
    results = []
    for mode in ("legacy", "steer"):
        input_ids = iter(("original", "correction"))
        monkeypatch.setattr(
            "yoke.cli.backend.daemon.session.uuid4",
            lambda: SimpleNamespace(hex=next(input_ids)),
        )
        provider = SteeringProvider()
        terminal.factory = lambda record: RuntimeAgent(
            provider, [], tool_root=Path(record.root)
        )
        backend = terminal.backend(mode, root=tmp_path)
        # Avoid unrelated title-provider calls in this projection comparison.
        backend.connection.session_request("PATCH", json={"title": "fixed"})
        backend.submit("original", user_message=None)
        assert provider.entered.wait(5)
        if mode == "legacy":
            backend.interrupt()
        turn = backend.submit(
            "correction",
            user_message=None,
            delivery="steer" if mode == "steer" else "queue",
        )
        list(turn)
        assert turn.result().result.output == "corrected"
        entries = terminal.store.load(mode).conversation_entries
        # IDs and creation times are storage identities, not transcript content.
        identities = {entry.id: index for index, entry in enumerate(entries)}
        tree = []
        for entry in entries:
            value = entry.model_dump(mode="json")
            value["id"] = identities[entry.id]
            if entry.parent_id is not None:
                value["parent_id"] = identities[entry.parent_id]
            value.pop("created_at", None)
            tree.append(value)
        results.append(
            (
                json.dumps(
                    provider.records, sort_keys=True, separators=(",", ":")
                ).encode(),
                tree,
            )
        )
    assert results[0][0] == results[1][0]
    assert results[0][1] == results[1][1]
