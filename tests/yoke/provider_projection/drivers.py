"""Drive the existing CLI helpers and HTTP routes without loop internals."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from io import StringIO
from threading import Event
from typing import Any

from fastapi.testclient import TestClient
from rich.console import Console

from yoke.agent.loop import AgentStoppedError
from yoke.agent.models import ConversationEntry, Message
from yoke.agent.multimodal import build_image_user_message
from yoke.cli.config import CLIArgs
from yoke.cli.image_input import build_user_message
from yoke.cli.interactive.skill_commands import handle_skill_load
from yoke.cli.runtime.base import execute_turn
from yoke.cli.runtime.compaction import force_compact_history
from yoke.cli.runtime.resume import project_resumed_session
from yoke.cli.runtime.session import (
    bind_agent_provider_session,
    create_active_session,
    fork_active_session,
    persist_session_state,
    save_active_session,
)
from yoke.cli.runtime.title import session_usage_metric_context
from yoke.cli.runtime.tree import navigate_session_tree
from yoke.http.app import HttpAppSettings, create_app
from yoke.session import SessionStore
from yoke.session.title import generate_session_title
from yoke.session.interrupt import interrupted_turn_snapshot

from .recording import Gate
from .runtime import World


class InProcess:
    """Use CLI session construction, execution, persistence, resume and commands."""

    def __init__(
        self, world: World, session_id: str = "oracle-session", *, fresh: bool = True
    ):
        self.world = world
        self.session_id = session_id
        # SessionStore() uses YOKE_SESSION_DIR, just as create_active_session does.
        self.active = create_active_session(
            CLIArgs(session=session_id, root=str(world.root)),
            root=world.root,
        )
        self.agent = world.agent(self.active.record)
        bind_agent_provider_session(self.agent, session_id)
        if fresh:
            self.active.title = "Oracle fixture"
            save_active_session(self.active, [], agent=self.agent)
        projection = project_resumed_session(self.active.record)
        self.agent.load_conversation(
            conversation_entries=projection.active_entries,
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
                with session_usage_metric_context(self.active, text):
                    result = execute_turn(
                        self.agent,
                        text,
                        self.active.messages(),
                        stderr=StringIO(),
                        conversation_entries=self.active.active_entries(),
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
                        after_tool_result_appended=checkpoint,
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
        with session_usage_metric_context(self.active, ""):
            result = force_compact_history(
                self.agent,
                self.active.messages(),
                conversation_entries=self.active.active_entries(),
            )
        assert result is not None
        self.persist()

    def activate(self) -> None:
        handle_skill_load(
            "/skill oracle",
            self.agent,
            self.active,
            self.active.messages(),
            Console(file=StringIO()),
        )

    def navigate(self, target: str, *, summarize: bool = False) -> None:
        with session_usage_metric_context(self.active, ""):
            navigate_session_tree(self.active, self.agent, target, summarize=summarize)

    def fork(self) -> None:
        self.active = fork_active_session(
            self.active, self.agent, self.active.messages()
        )
        self.session_id = self.active.id

    def title(self) -> None:
        with session_usage_metric_context(self.active, ""):
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
