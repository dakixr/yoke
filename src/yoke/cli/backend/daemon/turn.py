"""One admitted daemon input and its terminal-facing completion."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from threading import Event

from yoke.agent.loop import AgentResult
from yoke.agent.models import Message
from yoke.cli.backend.connection import SessionConnection
from yoke.cli.backend.events import BackendEvent, EventStream
from yoke.cli.backend.headless import input_output, projected_message
from yoke.cli.backend.models import TurnFailure, TurnStopped, TurnSuccess
from yoke.http.models.session import MessageListResponse


def transcript(connection: SessionConnection) -> list[Message]:
    values: list[Message] = []
    cursor: str | None = None
    while True:
        params: dict[str, str | int] = {"order": "asc", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        page = MessageListResponse.model_validate(
            connection.session_request("GET", "/message", params=params)
        )
        for item in page.data:
            message = projected_message(item.model_dump(by_alias=True))
            if message is not None:
                values.append(message)
        cursor = page.cursor.next
        if not cursor:
            return values


class DaemonSubmittedTurn:
    """Filter the session feed by input identity, including after recovery."""

    def __init__(
        self,
        connection: SessionConnection,
        input_id: str,
        stream: EventStream,
        unsubscribe: Callable[[], None],
    ) -> None:
        self.connection = connection
        self.input_id = input_id
        self._stream = stream
        self._unsubscribe = unsubscribe
        self._settled: dict[str, object] = {}
        self._outcome: TurnSuccess | TurnFailure | TurnStopped | None = None
        self._done = Event()

    @classmethod
    def failed(
        cls, connection: SessionConnection, input_id: str, error: Exception
    ) -> DaemonSubmittedTurn:
        stream = EventStream()
        stream.close()
        turn = cls(connection, input_id, stream, lambda: None)
        turn._outcome = TurnFailure(error=error)
        return turn

    def __iter__(self) -> Iterator[BackendEvent]:
        try:
            for event in self._stream:
                payload = event["payload"]
                if payload.get("inputID") != self.input_id:
                    continue
                if event["type"].startswith("session.prompt."):
                    continue
                if event["type"] == "session.runtime.failed":
                    self._settled["error"] = payload.get("error", "Daemon turn failed.")
                    self._settled.setdefault("outcome", "failed")
                    break
                if event["type"] == "session.input.changed" and payload.get(
                    "phase"
                ) in {"settled", "removed"}:
                    self._settled.update(payload)
                    if payload["phase"] == "removed":
                        self._settled["outcome"] = "stopped"
                    # Failed settlement precedes the actionable runtime error.
                    # Workspace rejection leaves the input paused, unsettled.
                    if payload.get("outcome") != "failed":
                        break
                yield event
        finally:
            self._unsubscribe()
            self.connection.view_store.forget_local(self.input_id)
            self._done.set()

    def close(self) -> None:
        """Detach this renderer without cancelling its durable daemon input."""
        self._unsubscribe()
        self.connection.view_store.forget_local(self.input_id)

    def result(self) -> TurnSuccess | TurnFailure | TurnStopped:
        self._done.wait()
        if self._outcome is not None:
            return self._outcome
        outcome = self._settled.get("outcome")
        messages = transcript(self.connection)
        result = AgentResult(
            output=input_output(self.connection, self.input_id),
            messages=messages,
            iterations=0,
            status="stopped" if outcome in {"stopped", "recovered"} else "completed",
            conversation_entries=[],
        )
        if outcome in {"stopped", "recovered"}:
            self._outcome = TurnStopped(result=result)
        elif outcome == "failed" or outcome is None:
            self._outcome = TurnFailure(
                error=RuntimeError(
                    str(self._settled.get("error", "Daemon turn failed."))
                ),
                messages=messages,
                conversation_entries=[],
            )
        else:
            self._outcome = TurnSuccess(result=result)
        return self._outcome
