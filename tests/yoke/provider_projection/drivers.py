"""Drive the SDK reference and HTTP routes without loop internals."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from threading import Event
from typing import Any

from fastapi.testclient import TestClient

from yoke.agent.loop import AgentStoppedError
from yoke.agent.models import ConversationEntry, Message
from yoke.agent.multimodal import build_image_user_message
from yoke.cli.image_input import build_user_message
from yoke.agent.compaction import force_compact_agent
from tests.yoke.session_support import (
    StoredSession,
    persist_session_state,
    save_active_session,
)
from yoke.agent.session_tree import (
    SessionTree,
    ConversationProjection,
    parse_memory_message,
)
from yoke.agent.skills import activate_skills
from yoke.ai.providers.usage_context import usage_metric_context
from yoke.session.repository import fallback_session_title
from yoke.session.repository.utils import new_unique_session_id
from yoke.ai.providers.base import complete_with_cancel
from yoke.http.app import HttpAppSettings, create_app
from yoke.session import SessionStore
from yoke.session.title import generate_session_title
from yoke.session.interrupt import interrupted_turn_snapshot

from .recording import Gate
from .runtime import World


class InProcess:
    """Use the SDK library and test-owned SessionStore records."""

    def __init__(
        self, world: World, session_id: str = "oracle-session", *, fresh: bool = True
    ):
        self.world = world
        self.session_id = session_id
        store = SessionStore(world.directory)
        record = store.load(session_id)
        if fresh:
            record = store.save(session_id, [], root=world.root)
        self.active = StoredSession(session_id, world.root, store, record)
        self.agent = world.agent(self.active.record)
        if fresh:
            self.active.title = "Oracle fixture"
            save_active_session(self.active, [], agent=self.agent)
        active = self.active.active_entries()
        source = (
            self.active.record.conversation_entries
            if any(
                entry.message is not None
                and entry.message.role == "user"
                and parse_memory_message(entry.message.plain_text_content or "")
                is not None
                for entry in active
            )
            else active
        )
        projection = SessionTree.restore(source, self.active.record.leaf_id).project(
            ConversationProjection()
        )
        self.agent.load_conversation(
            conversation_entries=projection.runtime_entries,
            active_skills=self.active.record.active_skills,
        )

    def persist(self) -> None:
        persist_session_state(
            self.active,
            self.agent,
            self.agent.messages,
            conversation_entries=self.agent.conversation_entries,
        )

    def prompt(
        self, text: str, *, image: bool = False, gate: Gate | None = None
    ) -> None:
        stopped = Event()

        def checkpoint(
            messages: list[Message], entries: list[ConversationEntry]
        ) -> None:
            persist_session_state(
                self.active, self.agent, messages, conversation_entries=entries
            )

        def run() -> None:
            try:
                with usage_metric_context(
                    surface="cli",
                    session_id=self.session_id,
                    session_title=self.active.title or fallback_session_title(text),
                ):
                    result = self.agent.run(
                        text,
                        user_message=(
                            build_image_user_message(
                                text,
                                image_paths=(self.world.root / "tiny.png",),
                            )
                            if image and self.world.scenario.startswith("local_images_")
                            else build_user_message(
                                text,
                                image_paths=(self.world.root / "tiny.png",)
                                if image
                                else (),
                            )
                        ),
                        stop_requested=stopped.is_set,
                        after_tool_result_appended=lambda context: checkpoint(
                            context.messages, list(context.conversation_log.entries)
                        ),
                    )
                assert result.status == ("stopped" if gate else "completed")
            except AgentStoppedError:
                assert gate is not None
                _, entries = interrupted_turn_snapshot(
                    messages=self.agent.messages,
                    entries=self.agent.conversation_entries,
                    user_message=None,
                )
                self.agent.load_conversation(conversation_entries=entries)
            self.persist()

        if gate is None:
            run()
        else:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(run)
                try:
                    assert gate.entered.wait(5)
                    assert any(
                        e.kind == "tool_result"
                        for e in SessionStore(self.world.directory)
                        .load(self.session_id)
                        .conversation_entries
                    )
                    stopped.set()
                finally:
                    gate.release.set()
                future.result(timeout=5)

    def compact(self) -> None:
        with usage_metric_context(
            surface="cli",
            session_id=self.session_id,
            session_title=self.active.title or fallback_session_title(""),
        ):
            result = force_compact_agent(
                self.agent,
                self.active.messages(),
                conversation_entries=self.active.active_entries(),
            )
        assert result is not None
        self.persist()

    def activate(self) -> None:
        assert self.agent.skill_registry is not None
        activation = activate_skills(
            registry=self.agent.skill_registry,
            active_skills=self.agent.active_skills,
            names=["oracle"],
        )
        tree = SessionTree.restore(
            self.agent.conversation_entries, self.active.record.leaf_id
        )
        tree.append_active_skills(activation.activated_skills)
        exported = tree.export_for_persistence()
        self.agent.load_conversation(
            conversation_entries=exported.entries,
            active_skills=activation.active_skills,
        )
        self.persist()

    def navigate(self, target: str, *, summarize: bool = False) -> None:
        tree = SessionTree.restore(
            self.active.record.conversation_entries, self.active.record.leaf_id
        )
        ref = tree.ref_from_persisted_id(target)
        preview = tree.preview_navigation(ref, include_abandoned=summarize)
        summary = None
        if summarize and preview.abandoned:
            rendered = "\n\n".join(
                ""
                if entry.kind == "tool_result"
                else f"[{entry.kind}/{entry.message.role}] {entry.message.text_content() or ''}"
                if entry.message
                else f"[{entry.kind}] {entry.summary_text}"
                if entry.summary_text is not None
                else f"[{entry.kind}]"
                for entry in preview.abandoned
            )
            with usage_metric_context(
                surface="cli",
                session_id=self.session_id,
                session_title=self.active.title,
                call_kind="branch_summary",
            ):
                response = complete_with_cancel(
                    self.agent.provider,
                    [
                        Message.system(
                            "Summarize the following abandoned conversation branch so it can be "
                            "used as compact context on a new branch. Preserve concrete decisions, "
                            "files touched, errors, commands, and unresolved next steps."
                        ),
                        Message.user(rendered),
                    ],
                    [],
                )
            summary = (response.plain_text_content or "").strip() or None
        tree.navigate(ref, branch_summary=summary)
        exported = tree.export_for_persistence()
        save_active_session(
            self.active,
            [],
            conversation_entries=list(exported.entries),
            leaf_id=exported.leaf_id,
            agent=self.agent,
        )
        projection = tree.project(ConversationProjection())
        self.agent.load_conversation(
            conversation_entries=projection.runtime_entries,
            active_skills=self.active.record.active_skills,
        )

    def fork(self) -> None:
        self.persist()
        record = self.active.store.fork(
            self.active.id,
            root=self.active.root,
            new_session_id_value=new_unique_session_id(self.active.store.exists),
        )
        self.active = StoredSession(
            record.id, self.active.root, self.active.store, record, record.title
        )
        self.session_id = record.id
        bind = getattr(self.agent.provider, "set_session_id", None)
        assert callable(bind)
        bind(record.id)

    def title(self) -> None:
        with usage_metric_context(
            surface="cli",
            session_id=self.session_id,
            session_title=self.active.title or fallback_session_title(""),
        ):
            assert (
                generate_session_title(self.agent, self.agent.messages)
                == "Oracle title"
            )

    def close(self) -> None:
        self.agent.close()


class Daemon:
    """Exercise the real ASGI app and persistence with an injected public factory."""

    def __init__(
        self, world: World, session_id: str = "oracle-session", *, fresh: bool = True
    ):
        self.world = world
        self.session_id = session_id
        self.stack = ExitStack()
        self.client = self.stack.enter_context(
            TestClient(
                create_app(
                    HttpAppSettings(
                        auth_token="oracle-secret",
                        session_directory=world.directory,
                        agent_factory=world.agent,
                    )
                )
            )
        )
        if fresh:
            self.request(
                "/session",
                {
                    "id": session_id,
                    "title": "Oracle fixture",
                    "location": {"directory": str(world.root)},
                },
            )

    def request(
        self, route: str, data: dict[str, Any] | None = None, *, method: str = "POST"
    ) -> Any:
        response = self.client.request(
            method,
            f"/api/v1{route}",
            json=data,
            headers={"Authorization": "Bearer oracle-secret"},
        )
        assert response.is_success, response.text
        return response.json()["data"]

    def wait(self, expected_error: str | None = None) -> None:
        status = self.request(f"/session/{self.session_id}/wait?timeoutMs=5000")
        if expected_error:
            assert status["state"] == "error", status
            assert status["lastError"] == expected_error
            self.world.tape.observations.append(
                {"error": expected_error, "provider_calls": len(self.world.tape.calls)}
            )
        else:
            assert status["state"] == "idle", status

    def prompt(
        self,
        text: str,
        *,
        image: bool = False,
        gate: Gate | None = None,
        expected_error: str | None = None,
    ) -> None:
        attachments = []
        if image:
            uploaded = self.client.post(
                "/api/v1/upload",
                params={"sessionID": self.session_id, "purpose": "promptAttachment"},
                headers={"Authorization": "Bearer oracle-secret"},
                files={
                    "file": (
                        "tiny.png",
                        (self.world.root / "tiny.png").read_bytes(),
                        "image/png",
                    )
                },
            )
            assert uploaded.is_success, uploaded.text
            attachments = [
                {
                    "type": "file",
                    "uri": uploaded.json()["data"]["uri"],
                    "name": "tiny.png",
                    "mime": "image/png",
                }
            ]
        try:
            self.request(
                f"/session/{self.session_id}/prompt",
                {
                    "prompt": {"text": text, "attachments": attachments},
                },
            )
            if gate:
                assert gate.entered.wait(5)
                assert any(
                    e.kind == "tool_result"
                    for e in SessionStore(self.world.directory)
                    .load(self.session_id)
                    .conversation_entries
                )
                assert self.request(f"/session/{self.session_id}/interrupt")[
                    "interrupted"
                ]
        finally:
            if gate:
                gate.release.set()
        self.wait(expected_error)

    def select_images(self, enabled: bool) -> None:
        self.request(
            f"/session/{self.session_id}/selection",
            {
                "provider": "oracle",
                "model": "oracle-vision" if enabled else "oracle-model",
            },
        )

    def compact(self) -> None:
        self.request(f"/session/{self.session_id}/compact", {"reason": "manual"})
        self.wait()

    def activate(self) -> None:
        self.request(f"/session/{self.session_id}/skill/oracle/activate", {})

    def navigate(self, target: str, *, summarize: bool = False) -> None:
        tree = self.request(f"/session/{self.session_id}/tree", method="GET")
        result = self.request(
            f"/session/{self.session_id}/tree/navigate",
            {
                "expectedRevision": tree["revision"],
                "targetID": target,
                "branchSummary": "Branch handoff." if summarize else None,
            },
        )
        assert result["summaryAdded"] == summarize

    def fork(self) -> None:
        self.session_id = self.request(f"/session/{self.session_id}/fork", {})["id"]

    def title(self) -> None:
        assert (
            self.request(f"/session/{self.session_id}/title/regenerate")["title"]
            == "Oracle title"
        )

    def close(self) -> None:
        self.stack.close()
