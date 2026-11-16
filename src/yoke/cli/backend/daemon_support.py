"""Presentation and inspection adapters for the daemon-backed CLI."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, RLock
from typing import Any, Callable, Iterator, TYPE_CHECKING
from types import SimpleNamespace

from yoke.agent.loop import AgentResult
from yoke.agent.models import Message
from yoke.cli.backend.connection import DaemonRequestError
from yoke.cli.backend.events import BackendEvent
from yoke.cli.backend.models import TurnFailure, TurnStopped, TurnSuccess

if TYPE_CHECKING:
    from yoke.cli.backend.daemon import DaemonSessionBackend


@dataclass(slots=True)
class DaemonSessionRecord:
    """Record-shaped metadata used by existing command renderers."""

    title: str | None = None
    pinned: bool = False
    provider_name: str | None = None
    model_id: str | None = None
    reasoning_effort: str | None = None
    context_window_tokens: int | None = None
    created_at: str | None = None
    updated_at: str | None = None
    conversation_entries: list[Any] = field(default_factory=list)
    active_skills: list[Any] = field(default_factory=list)
    leaf_id: str | None = None


class DaemonActiveSession:
    """Small ``ActiveSession`` compatible view backed by HTTP reads."""

    def __init__(self, backend: DaemonSessionBackend, data: dict[str, Any]) -> None:
        self.backend = backend
        self.id = backend.connection.session_id
        self.root = Path(data.get("location", {}).get("directory", Path.cwd()))
        self.title = data.get("title")
        self.save_lock = RLock()
        selection = data.get("selection", {})
        tree = data.get("tree", {})
        timing = data.get("time", {})
        self.record = DaemonSessionRecord(
            title=self.title,
            pinned=bool(data.get("pinned", False)),
            provider_name=selection.get("provider"),
            model_id=selection.get("model"),
            reasoning_effort=selection.get("reasoningEffort"),
            created_at=timing.get("created"),
            updated_at=timing.get("updated"),
            leaf_id=tree.get("leafID"),
        )

    def refresh(self) -> None:
        data = self.backend.connection.session_request("GET")["data"]
        self.title = data.get("title")
        self.record.title = self.title
        self.record.pinned = bool(data.get("pinned", False))
        selection = data.get("selection", {})
        self.record.provider_name = selection.get("provider")
        self.record.model_id = selection.get("model")
        self.record.reasoning_effort = selection.get("reasoningEffort")
        self.record.leaf_id = data.get("tree", {}).get("leafID")

    def active_entry_refs(self) -> list[Any]:
        return []

    def active_entries(self) -> list[Any]:
        return []

    def messages(self) -> list[Message]:
        return self.backend.messages()


class DaemonSubmittedTurn:
    """An admitted daemon turn exposed as the UI's iterable turn object."""

    def __init__(
        self,
        backend: DaemonSessionBackend,
        prompt: str,
        input_id: str,
        stream: Any,
        unsubscribe: Callable[[], None],
    ) -> None:
        self.backend = backend
        self.prompt = prompt
        self.input_id = input_id
        self._stream = stream
        self._unsubscribe = unsubscribe
        self._events: list[BackendEvent] = []
        self._outcome: TurnSuccess | TurnFailure | TurnStopped | None = None
        self._done = Event()

    def __iter__(self) -> Iterator[BackendEvent]:
        try:
            for event in self._stream:
                payload = event["payload"]
                event_input = payload.get("inputID")
                if event_input not in (None, self.input_id):
                    continue
                if event["type"] in {
                    "session.prompt.admitted",
                    "session.prompt.promoted",
                }:
                    continue
                self._events.append(event)
                yield event
                if (
                    event["type"] == "session.prompt.settled"
                    and event_input == self.input_id
                ):
                    break
        finally:
            self._unsubscribe()
            self._done.set()

    def result(self) -> TurnSuccess | TurnFailure | TurnStopped:
        self._done.wait()
        if self._outcome is not None:
            return self._outcome
        settled = next(
            (
                event["payload"]
                for event in reversed(self._events)
                if event["type"] == "session.prompt.settled"
            ),
            {},
        )
        outcome = settled.get("outcome")
        messages = self.backend.messages()
        output = ""
        for message in reversed(messages):
            if message.role == "assistant":
                output = message.final_text_content() or ""
                break
        result = AgentResult(
            output=output,
            messages=messages,
            iterations=0,
            status="stopped" if outcome in {"stopped", "recovered"} else "completed",
            conversation_entries=[],
        )
        if outcome in {"stopped", "recovered"}:
            self._outcome = TurnStopped(result=result)
        elif outcome == "failed":
            self._outcome = TurnFailure(
                error=RuntimeError(str(settled.get("error", "Daemon turn failed."))),
                messages=messages,
                conversation_entries=[],
            )
        else:
            self._outcome = TurnSuccess(result=result)
        return self._outcome


class DaemonToolInventory:
    def __init__(self, backend: DaemonSessionBackend) -> None:
        self.backend = backend

    def report(self) -> Any:
        return self.backend.connection.request(
            "GET", "tool", params={"sessionID": self.backend.connection.session_id}
        ).get("data")

    def active_names(self) -> set[str]:
        return {
            item["name"]
            for item in self.report() or []
            if isinstance(item, dict) and item.get("enabled")
        }

    def select(
        self,
        active_names: set[str],
        visible_names: set[str],
        report: Any,
        *,
        ordered_names: Any,
    ) -> None:
        del visible_names, report, ordered_names
        self.backend.connection.session_request(
            "PATCH", "/tool", json={"enabled": sorted(active_names), "disabled": []}
        )


class DaemonMcpInventory:
    def __init__(self, backend: DaemonSessionBackend, root: Path) -> None:
        self.backend = backend
        self.root = root

    def config(self) -> Any:
        return self.backend.connection.session_request(
            "GET", "/mcp", params={"includeTools": True}
        )

    def list_tools(self, server: Any) -> tuple[Any, ...]:
        del server
        return ()

    def set_enabled(self, server: Any, enabled: bool) -> None:
        self.backend.connection.session_request(
            "PATCH",
            f"/mcp/{server.name}",
            json={"enabled": enabled, "scope": "session"},
        )

    def toggle_tool(self, server: Any, tool_name: str) -> None:
        self.backend.connection.session_request(
            "PATCH",
            f"/mcp/{server.name}",
            json={"enabled_tools": [tool_name], "scope": "session"},
        )

    def refresh(self) -> None:
        self.backend.connection.session_request(
            "GET", "/mcp", params={"includeTools": True}
        )


class DaemonInspection:
    def __init__(self, backend: DaemonSessionBackend) -> None:
        self.backend = backend

    @property
    def available(self) -> bool:
        return True

    def snapshots(self) -> list[Any]:
        values = self.backend.connection.request(
            "GET", "process", params={"sessionID": self.backend.connection.session_id}
        ).get("data", [])
        return [
            SimpleNamespace(
                **{
                    **value,
                    "process_id": value.get("processID"),
                    "runtime_session_id": value.get("runtimeSessionID"),
                    "exit_code": value.get("exitCode"),
                }
            )
            for value in values
            if isinstance(value, dict)
        ]

    def subscribe(self, changed: Callable[[], None]) -> Callable[[], None]:
        del changed
        return lambda: None

    def agent_runs(self) -> list[dict[str, object]]:
        return self.backend.connection.request(
            "GET", "agent-run", params={"sessionID": self.backend.connection.session_id}
        ).get("data", [])


class DaemonToolInspection:
    def __init__(self, backend: DaemonSessionBackend) -> None:
        self.backend = backend

    def snapshots(self) -> list[dict[str, Any]]:
        return self.backend.connection.session_request("GET", "/tool-call").get(
            "data", []
        )

    def revision(self) -> tuple[int, int, int]:
        return (0, len(self.snapshots()), 0)

    def call(self, call_id: str) -> Any:
        try:
            return self.backend.connection.session_request(
                "GET", f"/tool-call/{call_id}"
            )["data"]
        except DaemonRequestError:
            return None

    def output(self, call_id: str, *, after_seq: int = 0) -> list[Any]:
        return self.backend.connection.session_request(
            "GET", f"/tool-call/{call_id}/output", params={"afterSeq": after_seq}
        ).get("data", [])

    def subscribe(self, changed: Callable[[], None]) -> Callable[[], None]:
        del changed
        return lambda: None
