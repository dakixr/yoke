from __future__ import annotations
from tests.yoke.http.fake_session_agent import FakeSessionAgent

# ruff: noqa: D100,D101,D102,D103,S101
import time
from collections import defaultdict
from pathlib import Path
from threading import Event, Lock

from fastapi.testclient import TestClient

from yoke.agent.loop import AgentResult
from yoke.agent.models import Message
from yoke.agent.session_tree import ConversationProjection, SessionTree
from yoke.ai.providers.usage_context import (
    UsageMetricContext,
    current_usage_metric_context,
)
from yoke.http.app import HttpAppSettings, create_app
from yoke.session import SessionRecord

TOKEN = "runtime-secret"


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


class FakeRuntimeController:
    def __init__(self) -> None:
        self.started: defaultdict[str, Event] = defaultdict(Event)
        self.release: defaultdict[str, Event] = defaultdict(Event)
        self.lock = Lock()
        self.running_sessions: set[str] = set()
        self.max_parallel_sessions = 0
        self.usage_contexts: dict[str, UsageMetricContext] = {}

    def factory(self, record: SessionRecord) -> FakeAgent:
        return FakeAgent(record, self)

    def enter(self, session_id: str) -> None:
        with self.lock:
            self.running_sessions.add(session_id)
            self.max_parallel_sessions = max(
                self.max_parallel_sessions,
                len(self.running_sessions),
            )

    def leave(self, session_id: str) -> None:
        with self.lock:
            self.running_sessions.discard(session_id)


class FakeAgent(FakeSessionAgent):
    supports_user_message = True
    supports_message_history = False

    def __init__(
        self, record: SessionRecord, controller: FakeRuntimeController
    ) -> None:
        super().__init__(record)
        self.record = record.model_copy(deep=True)
        self.controller = controller

    def run(
        self,
        prompt: str,
        *,
        user_message: Message | None = None,
        append_user_message: bool = True,
        on_event=None,
        stop_requested=None,
        **_kwargs,
    ) -> AgentResult:
        self.controller.usage_contexts[prompt] = current_usage_metric_context()
        self.controller.enter(self.record.id)
        self.controller.started[prompt].set()
        try:
            if prompt == "parallel-tools" and on_event is not None:
                on_event(
                    "tool_execution_start",
                    {
                        "iteration": 1,
                        "tool_name": "tool.first",
                        "tool_call_id": "call-first",
                        "tool_arguments": "{}",
                    },
                )
                on_event(
                    "tool_execution_start",
                    {
                        "iteration": 1,
                        "tool_name": "tool.second",
                        "tool_call_id": "call-second",
                        "tool_arguments": "{}",
                    },
                )
                on_event(
                    "tool_execution_end",
                    {
                        "iteration": 1,
                        "tool_name": "tool.first",
                        "tool_call_id": "call-first",
                        "ok": True,
                        "result": {"ok": True},
                    },
                )
                self.controller.started["parallel-tools-midpoint"].set()
            if prompt == "context-usage-live" and on_event is not None:
                on_event(
                    "model_end",
                    {
                        "phase": "commentary",
                        "content": "still working",
                        "iteration": 1,
                        "tool_calls": 1,
                        "usage": {
                            "input_tokens": 32_000,
                            "output_tokens": 400,
                            "total_tokens": 32_400,
                            "cached_input_tokens": 24_000,
                        },
                    },
                )
                self.controller.started["context-usage-live-measured"].set()
            while not self.controller.release[prompt].is_set():
                if stop_requested is not None and stop_requested():
                    return AgentResult(
                        output="",
                        messages=[],
                        iterations=0,
                        status="stopped",
                        conversation_entries=[],
                    )
                time.sleep(0.005)
            if prompt == "parallel-tools" and on_event is not None:
                on_event(
                    "tool_execution_end",
                    {
                        "iteration": 1,
                        "tool_name": "tool.second",
                        "tool_call_id": "call-second",
                        "ok": True,
                        "result": {"ok": True},
                    },
                )
            tree = SessionTree.restore(
                self.record.conversation_entries,
                self.record.leaf_id,
            )
            tree.append_message(user_message or Message.user(prompt))
            tree.append_message(Message.assistant(f"done:{prompt}"))
            view = tree.project(ConversationProjection())
            if on_event is not None:
                assistant_payload: dict[str, object] = {
                    "phase": "final_answer",
                    "content": f"done:{prompt}",
                    "iteration": 1,
                }
                if prompt == "context-usage":
                    on_event(
                        "model_end",
                        {
                            "phase": "final_answer",
                            "content": f"done:{prompt}",
                            "iteration": 1,
                            "tool_calls": 0,
                            "usage": {
                                "input_tokens": 32_000,
                                "output_tokens": 400,
                                "total_tokens": 32_400,
                                "cached_input_tokens": 24_000,
                            },
                        },
                    )
                on_event(
                    "assistant_message",
                    assistant_payload,
                )
            return AgentResult(
                output=f"done:{prompt}",
                messages=list(view.transcript_messages),
                iterations=1,
                conversation_entries=list(tree.entries),
            )
        finally:
            self.controller.leave(self.record.id)


def _create_session(client: TestClient, root: Path, session_id: str) -> None:
    response = client.post(
        "/api/v1/session",
        headers=_auth(),
        json={"id": session_id, "location": {"directory": str(root)}},
    )
    assert response.status_code == 200


def test_prompt_admission_is_idempotent_and_queue_patch_is_revision_checked(
    tmp_path: Path,
) -> None:
    controller = FakeRuntimeController()
    app = create_app(
        HttpAppSettings(
            auth_token=TOKEN,
            session_directory=tmp_path / "sessions",
            agent_factory=controller.factory,
            indexed_runtime_seed=False,
        )
    )
    with TestClient(app) as client:
        root = tmp_path / "repo"
        root.mkdir()
        _create_session(client, root, "session-a")
        body = {
            "id": "inp-fixed",
            "prompt": {"text": "later"},
            "delivery": "queue",
            "resume": False,
        }

        admitted = client.post(
            "/api/v1/session/session-a/prompt",
            headers=_auth(),
            json=body,
        )
        repeated = client.post(
            "/api/v1/session/session-a/prompt",
            headers=_auth(),
            json=body,
        )
        assert admitted.status_code == 200
        assert repeated.status_code == 200
        assert admitted.json() == repeated.json()
        assert admitted.json()["data"]["admittedSeq"] > 0

        conflict = client.post(
            "/api/v1/session/session-a/prompt",
            headers=_auth(),
            json={**body, "delivery": "steer"},
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "input_identity_conflict"

        queue = client.get("/api/v1/session/session-a/queue", headers=_auth())
        assert queue.json()["data"]["revision"] == 1
        stale = client.patch(
            "/api/v1/session/session-a/queue",
            headers=_auth(),
            json={
                "expectedRevision": 0,
                "operations": [{"op": "setPaused", "id": "inp-fixed", "paused": True}],
            },
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "queue_revision_conflict"

        edited = client.patch(
            "/api/v1/session/session-a/queue",
            headers=_auth(),
            json={
                "expectedRevision": 1,
                "operations": [
                    {
                        "op": "update",
                        "id": "inp-fixed",
                        "prompt": {"text": "edited later"},
                    },
                    {"op": "setPaused", "id": "inp-fixed", "paused": True},
                ],
            },
        )
        assert edited.status_code == 200
        assert edited.json()["data"]["revision"] == 2
        assert edited.json()["data"]["items"][0]["prompt"]["text"] == "edited later"
        assert edited.json()["data"]["items"][0]["paused"] is True
        assert not controller.started["edited later"].is_set()
