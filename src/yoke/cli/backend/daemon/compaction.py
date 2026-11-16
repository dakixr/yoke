"""Wait for a daemon compaction and return only its persisted presentation."""

from yoke.cli.backend.connection import SessionConnection
from yoke.cli.backend.daemon.turn import transcript
from yoke.cli.runtime.compaction import CompactionSnapshot
from yoke.http.models.session import WaitResponse


class DaemonCompactor:
    def __init__(self, connection: SessionConnection) -> None:
        self.connection = connection

    def run(self) -> CompactionSnapshot | None:
        events, unsubscribe = self.connection.events.subscribe()
        try:
            self.connection.session_request(
                "POST", "/compact", json={"reason": "manual"}
            )
            status = WaitResponse.model_validate(
                self.connection.session_request(
                    "POST", "/wait", params={"timeoutMs": 300_000}
                )
            ).data
            if status.state != "idle":
                raise ValueError(status.last_error or "Compaction has not finished.")
            for event in events:
                if event["type"] != "session.compaction.ended":
                    continue
                data = event["payload"]
                if not data.get("compacted"):
                    return None
                return CompactionSnapshot(
                    messages=transcript(self.connection),
                    payload={
                        "reason": "manual",
                        "input_tokens": data.get("inputTokens"),
                        "compacted_input_tokens": data.get("compactedInputTokens"),
                    },
                )
            raise ValueError("Daemon disconnected before compaction settled.")
        finally:
            unsubscribe()
