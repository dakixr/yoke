"""HTTP implementation of the interactive CLI backend."""

from __future__ import annotations

import mimetypes
from pathlib import Path
from threading import Event
from typing import Any, Callable
from uuid import uuid4
from types import SimpleNamespace

from yoke.agent.models import Message
from yoke.cli.backend.connection import DaemonRequestError, SessionConnection
from yoke.cli.backend.headless import projected_message
from yoke.cli.backend.models import PendingPrompt
from yoke.cli.backend.queue import LoadedPromptQueue
from yoke.cli.backend.daemon_queue import clear_queue, commit_queue, load_queue
from yoke.cli.backend.daemon_support import (
    DaemonActiveSession,
    DaemonSubmittedTurn,
    DaemonToolInventory,
    DaemonMcpInventory,
    DaemonInspection,
    DaemonToolInspection,
)
from yoke.cli.image_input import ImageAttachment


class DaemonSessionBackend:
    """HTTP implementation of the interactive ``SessionBackend`` seam."""

    def __init__(self, connection: SessionConnection, data: dict[str, Any]) -> None:
        self.connection = connection
        self.events = connection.events
        self._session = DaemonActiveSession(self, data)
        self._skills: list[Any] = []

    @property
    def session(self) -> DaemonActiveSession:
        return self._session

    @property
    def runtime_available(self) -> bool:
        return False

    def provider_label(self) -> str | None:
        selection = self.connection.session_request("GET")["data"].get("selection", {})
        provider, model = selection.get("provider"), selection.get("model")
        return f"{provider}:{model}" if provider and model else model or provider

    def thinking(self) -> tuple[str | None, tuple[str, ...]]:
        data = self.connection.session_request("GET")["data"].get("selection", {})
        levels: tuple[str, ...] = ()
        try:
            models = self.connection.request(
                "GET", "model", params={"provider": data.get("provider")}
            ).get("data", [])
            for model in models:
                if model.get("id") == data.get("model"):
                    levels = tuple(model.get("reasoningEfforts", ()))
                    break
        except Exception:
            pass
        return data.get("reasoningEffort"), levels

    def set_thinking(self, effort: str) -> None:
        selection = self.connection.session_request("GET")["data"].get("selection", {})
        self.connection.session_request(
            "POST",
            "/selection",
            json={
                "provider": selection.get("provider"),
                "model": selection.get("model"),
                "reasoningEffort": effort,
            },
        )
        self._session.record.reasoning_effort = effort

    def available_skills(self) -> list[Any]:
        return self.connection.request(
            "GET", "skill", params={"directory": str(self.session.root)}
        ).get("data", [])

    def messages(self) -> list[Message]:
        values: list[Message] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"order": "asc", "limit": 200}
            if cursor:
                params["cursor"] = cursor
            page = self.connection.session_request("GET", "/message", params=params)
            for item in page.get("data", []):
                message = projected_message(item)
                if message is not None:
                    values.append(message)
            cursor = page.get("cursor", {}).get("next")
            if not cursor:
                break
        return values

    def load_queue(self) -> LoadedPromptQueue:
        return load_queue(self)

    def commit_queue(
        self,
        prompts: list[PendingPrompt],
        pending_images: list[ImageAttachment],
        *,
        expected_revision: int | None,
    ) -> int:
        return commit_queue(
            self, prompts, pending_images, expected_revision=expected_revision
        )

    def clear_queue(self, *, expected_revision: int | None) -> None:
        clear_queue(self, expected_revision=expected_revision)

    def context_usage(
        self, prompt: str, messages: list[Message], *, conversation_entries: Any = None
    ) -> dict[str, int] | None:
        del prompt, messages, conversation_entries
        return self.connection.session_request("GET", "/context").get("data")

    def submit(
        self,
        prompt: str,
        messages: list[Message],
        *,
        entries: Any,
        stop_event: Event,
        user_message: Message | None,
        checkpoint: Any,
        starting_prompt: Any = None,
        delivery: str = "queue",
    ) -> DaemonSubmittedTurn:
        del messages, entries, user_message, checkpoint, starting_prompt
        setattr(stop_event, "daemon_backend", self)
        input_id = f"inp_{uuid4().hex}"
        stream, unsubscribe = self.events.subscribe()
        attachments: list[dict[str, str]] = []
        admission = {
            "id": input_id,
            "clientID": self.connection.owner,
            "delivery": delivery,
            "prompt": {"text": prompt, "attachments": attachments},
        }
        self.connection.session_request("POST", "/prompt", json=admission)
        return DaemonSubmittedTurn(self, prompt, input_id, stream, unsubscribe)

    def interrupt(self, stop_event: Event | None = None) -> None:
        del stop_event
        self.connection.session_request("POST", "/interrupt")

    def persist_metadata(self, reasoning_effort: str | None) -> None:
        if reasoning_effort is None:
            return
        self.set_thinking(reasoning_effort)

    def require_workspace(self, session: Any) -> None:
        del session

    def interrupt_snapshot(self, **kwargs: Any) -> tuple[list[Message], list[Any]]:
        return list(kwargs.get("messages", [])), list(kwargs.get("entries", []))

    @staticmethod
    def submission_context(callback: Callable[[], Any]) -> Callable[[], Any]:
        return callback

    def persist(
        self,
        messages: list[Message],
        *,
        conversation_entries: Any = None,
        turn_agent: Any = None,
    ) -> None:
        del messages, conversation_entries, turn_agent

    def sync_skills(self) -> None:
        return None

    def checkpoint_skills(self, turn_agent: Any) -> None:
        del turn_agent

    def promote_turn(self, turn_agent: Any) -> Any:
        return turn_agent

    def retire_turn(self, turn_agent: Any) -> None:
        del turn_agent

    def compact(
        self, messages: list[Message], *, conversation_entries: Any = None
    ) -> None:
        del messages, conversation_entries
        self.connection.session_request("POST", "/compact", json={"reason": "manual"})
        return None

    def pin(self, messages: list[Message]) -> bool:
        del messages
        value = not self._session.record.pinned
        data = self.connection.session_request("PATCH", json={"pinned": value})["data"]
        self._session.record.pinned = bool(data.get("pinned", value))
        return self._session.record.pinned

    def database_path(self) -> Path:
        return Path("daemon://yoke.db")

    def title(self, title: str, messages: list[Message]) -> None:
        del messages
        data = self.connection.session_request("PATCH", json={"title": title})["data"]
        self._session.title = data.get("title")
        self._session.record.title = self._session.title

    def regenerate_title(self, messages: list[Message]) -> str | None:
        del messages
        data = self.connection.session_request("POST", "/title/regenerate")["data"]
        self._session.title = data.get("title")
        self._session.record.title = self._session.title
        return self._session.title

    def fork(self, messages: list[Message]) -> DaemonActiveSession:
        del messages
        data = self.connection.session_request(
            "POST", "/fork", json={"location": {"directory": str(self.session.root)}}
        )["data"]
        self.connection.session_id = data["id"]
        self._session = DaemonActiveSession(self, data)
        return self._session

    def new(self) -> DaemonActiveSession:
        data = self.connection.request(
            "POST",
            "session",
            json={
                "location": {"directory": str(self.session.root)},
                "client": self.connection.client_config(),
            },
        )["data"]
        self.connection.session_id = data["id"]
        self._session = DaemonActiveSession(self, data)
        return self._session

    def tree(self) -> list[Any]:
        return (
            self.connection.session_request("GET", "/tree")
            .get("data", {})
            .get("entries", [])
        )

    def label_entry(self, entry_id: str, label: str) -> None:
        tree = self.connection.session_request("GET", "/tree")["data"]
        self.connection.session_request(
            "PATCH",
            f"/tree/{entry_id}",
            json={"expectedRevision": tree.get("revision", 0), "label": label},
        )

    def navigate(
        self,
        entry_id: str,
        *,
        summarize: bool = False,
        custom_instructions: str | None = None,
    ) -> Any:
        tree = self.connection.session_request("GET", "/tree")["data"]
        result = self.connection.session_request(
            "POST",
            "/tree/navigate",
            json={
                "expectedRevision": tree.get("revision", 0),
                "targetID": entry_id,
                "branchSummary": custom_instructions if summarize else None,
            },
        )["data"]
        return SimpleNamespace(
            messages=self.messages(),
            active_session=self.session,
            editor_text=result.get("editorText"),
            summary_created=result.get("summaryAdded", False),
        )

    def tools(self) -> DaemonToolInventory:
        return DaemonToolInventory(self)

    def mcp(self, root: Path) -> DaemonMcpInventory:
        return DaemonMcpInventory(self, root)

    def processes(self) -> DaemonInspection:
        return DaemonInspection(self)

    def tool_inspection(self) -> DaemonToolInspection:
        return DaemonToolInspection(self)

    def activate_skill(self, name: str, messages: list[Message]) -> str | None:
        del messages
        try:
            self.connection.session_request("POST", f"/skill/{name}/activate", json={})
        except DaemonRequestError as exc:
            return str(exc)
        return None

    @staticmethod
    def models(args: Any) -> list[dict[str, Any]]:
        del args
        return []

    def switch_model(
        self,
        qualified_model_id: str,
        reasoning_effort: str | None,
        *,
        args: Any,
        messages: list[Message],
    ) -> tuple[Any, list[Message], Any]:
        del args, messages
        provider, _, model = qualified_model_id.partition(":")
        self.connection.session_request(
            "POST",
            "/selection",
            json={
                "provider": provider,
                "model": model,
                "reasoningEffort": reasoning_effort,
            },
        )
        return (
            SimpleNamespace(
                provider_name=provider,
                model_id=model,
                reasoning_effort=reasoning_effort,
            ),
            self.messages(),
            None,
        )


def upload_daemon_image(
    connection: SessionConnection, image: ImageAttachment
) -> dict[str, str]:
    """Upload a clipboard/local image with the CLI projection marker."""
    mime = mimetypes.guess_type(image.path.name)[0] or "application/octet-stream"
    with image.path.open("rb") as handle:
        uploaded = connection.request(
            "POST",
            "upload",
            params={"sessionID": connection.session_id},
            files={"file": (image.path.name, handle, mime)},
        )["data"]
    return {
        "type": "file",
        "uri": uploaded["uri"],
        "name": image.path.name,
        "mime": mime,
        "imageProjection": "cli",
    }
