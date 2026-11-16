"""HTTP implementation of the interactive CLI backend."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
from threading import Event, Lock
from typing import Literal, cast
from uuid import uuid4

import httpx

from yoke.agent.models import ConversationEntry, Message
from yoke.agent.provider_selection import ProviderSessionState
from yoke.cli.backend.connection import DaemonRequestError, SessionConnection
from yoke.cli.backend.daemon.catalog import (
    DaemonMcpInventory,
    DaemonModelCatalog,
    DaemonToolInventory,
)
from yoke.cli.backend.daemon.inspection import DaemonInspection, DaemonToolInspection
from yoke.cli.backend.daemon.queue import DaemonPromptQueue
from yoke.cli.backend.daemon.presentation import DaemonActiveSession
from yoke.cli.backend.daemon.turn import DaemonSubmittedTurn, transcript
from yoke.cli.backend.models import PendingPrompt
from yoke.cli.backend.values import LoadedPromptQueue
from yoke.cli.backend.values import Checkpoint
from yoke.cli.config import CLIArgs
from yoke.cli.image_input import ImageAttachment
from yoke.cli.providers.catalog import ProviderModelChoice
from yoke.cli.backend.values import CompactionSnapshot
from yoke.cli.runtime.tree_view import TreeNode
from yoke.cli.backend.values import TreeNavigationResult
from yoke.cli.runtime import ActiveSession
from yoke.http.models.session import (
    SessionInfo,
    SessionCreateResponse,
    SessionResponse,
)
from yoke.http.models.skill import SkillInfo, SkillListResponse


class DaemonSessionBackend:
    """Own HTTP operations and local rendering identities for one terminal."""

    def __init__(
        self, connection: SessionConnection, data: dict[str, object] | SessionInfo
    ) -> None:
        self.connection = connection
        self.events = connection.events
        self._session = DaemonActiveSession(self, SessionInfo.model_validate(data))
        self._queue = DaemonPromptQueue(connection)
        self._local_inputs: set[str] = set()
        self._input_lock = Lock()

    @property
    def session(self) -> DaemonActiveSession:
        return self._session

    def select_session(self, session: DaemonActiveSession) -> None:
        self._session = session

    @property
    def runtime_available(self) -> bool:
        return False

    def refresh_session(self) -> None:
        self.session.refresh()

    def conversation_entry_count(self) -> int:
        return self.session.record.entry_count

    def provider_label(self) -> str | None:
        selection = self.session.record
        provider, model = selection.provider_name, selection.model_id
        return f"{provider}:{model}" if provider and model else model or provider

    def thinking(self) -> tuple[str | None, tuple[str, ...]]:
        return DaemonModelCatalog(self.connection).thinking(
            self.session.root, self.session.record
        )

    def set_thinking(self, effort: str) -> None:
        self.connection.session_request(
            "POST",
            "/selection",
            json={
                "provider": self.session.record.provider_name,
                "model": self.session.record.model_id,
                "reasoningEffort": effort,
            },
        )
        self.refresh_session()

    def available_skills(self) -> list[SkillInfo]:
        return SkillListResponse.model_validate(
            self.connection.request(
                "GET", "skill", params={"directory": str(self.session.root)}
            )
        ).data

    def messages(self) -> list[Message]:
        return transcript(self.connection)

    def load_queue(self) -> LoadedPromptQueue:
        return self._queue.load()

    def commit_queue(
        self,
        prompts: list[PendingPrompt],
        pending_images: list[ImageAttachment],
        *,
        expected_revision: int | None,
    ) -> int:
        return self._queue.commit(
            prompts, pending_images, expected_revision=expected_revision
        )

    def clear_queue(self, *, expected_revision: int | None) -> None:
        self._queue.clear(expected_revision=expected_revision)

    def context_usage(
        self,
        prompt: str,
        messages: list[Message],
        *,
        conversation_entries: Sequence[ConversationEntry] | None = None,
    ) -> dict[str, int] | None:
        del prompt, messages, conversation_entries
        usage = SessionResponse.model_validate(
            self.connection.session_request("GET")
        ).data.context_usage
        return (
            {key: value for key, value in usage.items() if isinstance(value, int)}
            if usage
            else None
        )

    def owns_input(self, input_id: str) -> bool:
        with self._input_lock:
            return input_id in self._local_inputs

    def submit(
        self,
        prompt: str,
        messages: list[Message],
        *,
        entries: list[ConversationEntry],
        stop_event: Event,
        user_message: Message | None,
        checkpoint: Checkpoint,
        starting_prompt: tuple[PendingPrompt, int] | None = None,
        delivery: Literal["queue", "steer"] = "queue",
    ) -> DaemonSubmittedTurn:
        del messages, entries, checkpoint, starting_prompt
        from yoke.cli.backend.daemon.images import upload_message_images

        setattr(stop_event, "daemon_backend", self)
        input_id = f"inp_{uuid4().hex}"
        # Register before admission: the SSE echo may arrive before POST returns.
        with self._input_lock:
            self._local_inputs.add(input_id)
        attachments = upload_message_images(self.connection, user_message)
        stream, unsubscribe = self.events.subscribe()
        admission = {
            "id": input_id,
            "clientID": self.connection.owner,
            "delivery": delivery,
            "prompt": {
                "text": prompt,
                "attachments": attachments,
            },
        }
        try:
            try:
                self.connection.start()
                self.connection.session_request("POST", "/prompt", json=admission)
            except httpx.HTTPError:
                # Admission alone is safe to replay, using exactly the same ID.
                self.connection.session_request("GET", "/client")
                self.connection.start()
                self.connection.session_request("POST", "/prompt", json=admission)
        except (DaemonRequestError, httpx.HTTPError, OSError, TimeoutError) as exc:
            unsubscribe()
            return DaemonSubmittedTurn.failed(self.connection, input_id, exc)
        return DaemonSubmittedTurn(self.connection, input_id, stream, unsubscribe)

    def interrupt(self, stop_event: Event | None = None) -> None:
        del stop_event
        self.connection.session_request("POST", "/interrupt")

    def persist_metadata(self, reasoning_effort: str | None) -> None:
        if (
            reasoning_effort is not None
            and reasoning_effort != self.session.record.reasoning_effort
        ):
            self.set_thinking(reasoning_effort)

    @staticmethod
    def submission_context[T](callback: Callable[[], T]) -> Callable[[], T]:
        return callback

    def persist(
        self,
        messages: list[Message],
        *,
        conversation_entries: Sequence[ConversationEntry] | None = None,
        turn_agent: object | None = None,
    ) -> None:
        del messages, conversation_entries, turn_agent

    def sync_skills(self) -> None:
        return None

    def checkpoint_skills(self, turn_agent: object) -> None:
        del turn_agent

    def promote_turn(self, turn_agent: object) -> object:
        return turn_agent

    def retire_turn(self, turn_agent: object | None) -> None:
        del turn_agent

    def compact(
        self,
        messages: list[Message],
        *,
        conversation_entries: Sequence[ConversationEntry] | None = None,
    ) -> CompactionSnapshot | None:
        del messages, conversation_entries
        from yoke.cli.backend.daemon.compaction import DaemonCompactor

        compacted = DaemonCompactor(self.connection).run()
        self.refresh_session()
        return compacted

    def pin(self, messages: list[Message]) -> bool:
        del messages
        self._update(
            self.connection.session_request(
                "PATCH", json={"pinned": not self.session.record.pinned}
            )
        )
        return self.session.record.pinned

    def database_path(self) -> Path:
        path = self.session.database_path
        if path is None:
            raise ValueError("Daemon did not report its database path.")
        return path

    def title(self, title: str, messages: list[Message]) -> None:
        del messages
        self._update(self.connection.session_request("PATCH", json={"title": title}))

    def regenerate_title(self, messages: list[Message]) -> str | None:
        del messages
        self._update(self.connection.session_request("POST", "/title/regenerate"))
        return self.session.title

    def _update(self, response: dict[str, object]) -> None:
        self.session.update(SessionResponse.model_validate(response).data)

    def fork(self, messages: list[Message]) -> DaemonActiveSession:
        del messages
        data = SessionResponse.model_validate(
            self.connection.session_request(
                "POST",
                "/fork",
                json={"location": {"directory": str(self.session.root)}},
            )
        ).data
        self.connection.session_id = data.id
        self._session = DaemonActiveSession(self, data)
        return self.session

    def new(self) -> DaemonActiveSession:
        data = SessionCreateResponse.model_validate(
            self.connection.request(
                "POST",
                "session",
                json={
                    "location": {"directory": str(self.session.root)},
                    "client": self.connection.client_config(),
                },
            )
        ).data
        self.connection.session_id = data.id
        self._session = DaemonActiveSession(self, data)
        return self.session

    def tree(self) -> list[TreeNode]:
        from yoke.cli.backend.daemon.tree import DaemonTree

        self.refresh_session()
        return DaemonTree(self.connection).roots()

    def label_entry(self, entry_id: str, label: str) -> None:
        from yoke.cli.backend.daemon.tree import DaemonTree

        DaemonTree(self.connection).label(entry_id, label)

    def navigate(
        self,
        entry_id: str,
        *,
        summarize: bool = False,
        custom_instructions: str | None = None,
    ) -> TreeNavigationResult:
        from yoke.cli.backend.daemon.tree import DaemonTree

        result = DaemonTree(self.connection).navigate(
            entry_id,
            branch_summary=custom_instructions if summarize else None,
        )
        self.refresh_session()
        return TreeNavigationResult(
            messages=self.messages(),
            active_session=cast(ActiveSession, self.session),
            editor_text=result.editor_text,
            summary_created=result.summary_added,
        )

    def tools(self) -> DaemonToolInventory:
        return DaemonToolInventory(self.connection)

    def mcp(self, root: Path) -> DaemonMcpInventory:
        del root
        return DaemonMcpInventory(self.connection)

    def processes(self) -> DaemonInspection:
        return DaemonInspection(self.connection)

    def tool_inspection(self) -> DaemonToolInspection:
        return DaemonToolInspection(self.connection)

    def activate_skill(self, name: str, messages: list[Message]) -> str | None:
        del messages
        try:
            self.connection.session_request("POST", f"/skill/{name}/activate", json={})
        except DaemonRequestError as exc:
            return str(exc)
        return None

    def models(self, args: CLIArgs) -> list[ProviderModelChoice]:
        del args
        return DaemonModelCatalog(self.connection).choices(self.session.root)

    def switch_model(
        self,
        qualified_model_id: str,
        reasoning_effort: str | None,
        *,
        args: CLIArgs,
        messages: list[Message],
    ) -> tuple[ProviderSessionState, list[Message], OSError | None]:
        del args
        provider, _, model = qualified_model_id.partition(":")
        try:
            self.connection.session_request(
                "POST",
                "/selection",
                json={
                    "provider": provider,
                    "model": model,
                    "reasoningEffort": reasoning_effort,
                },
            )
        except DaemonRequestError as exc:
            error = exc.body.get("error", {})
            if error.get("code") != "selection_metadata_not_saved":
                raise
            details = error["details"]
            state = ProviderSessionState(
                provider_name=details["provider"],
                model_id=details["model"],
                reasoning_effort=details["reasoningEffort"],
                context_window_tokens=details["contextWindowTokens"],
            )
            return state, messages, OSError(str(exc))
        self.refresh_session()
        record = self.session.record
        return (
            ProviderSessionState(
                provider_name=record.provider_name,
                model_id=record.model_id,
                reasoning_effort=record.reasoning_effort,
                context_window_tokens=record.context_window_tokens,
            ),
            self.messages(),
            None,
        )
