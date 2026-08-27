from __future__ import annotations

# ruff: noqa: D100,D101,D102,D103,S101

import asyncio
from collections import defaultdict
from io import BytesIO
from pathlib import Path
from threading import Event
from threading import Lock
import time

import pytest
from fastapi import UploadFile
from fastapi.testclient import TestClient
from PIL import Image
from starlette.datastructures import Headers

from yoke.agent.loop import AgentResult
from yoke.agent.models import Message
from yoke.agent.session_tree import ConversationProjection
from yoke.agent.session_tree import SessionTree
from yoke.http.app import HttpAppSettings
from yoke.http.app import create_app
from yoke.http.services.upload_service import UploadService
from yoke.session import SessionRecord
from yoke.session import SessionStore


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


class FakeAgent:
    supports_user_message = True

    def __init__(self, record: SessionRecord, controller: FakeRuntimeController) -> None:
        self.record = record.model_copy(deep=True)
        self.controller = controller

    def run(
        self,
        prompt: str,
        *,
        user_message: Message | None = None,
        on_event=None,
        stop_requested=None,
    ) -> AgentResult:
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
                on_event(
                    "assistant_message",
                    {"phase": "final_answer", "content": f"done:{prompt}"},
                )
            return AgentResult(
                output=f"done:{prompt}",
                messages=list(view.transcript_messages),
                iterations=1,
                conversation_entries=list(tree.entries),
            )
        finally:
            self.controller.leave(self.record.id)


def test_parallel_tool_activity_stays_running_until_all_tools_finish(
    tmp_path: Path,
) -> None:
    controller = FakeRuntimeController()
    app = create_app(
        HttpAppSettings(
            auth_token=TOKEN,
            session_directory=tmp_path / "sessions",
            agent_factory=controller.factory,
        )
    )
    with TestClient(app) as client:
        root = tmp_path / "repo"
        root.mkdir()
        _create_session(client, root, "parallel-session")
        admitted = client.post(
            "/api/v1/session/parallel-session/prompt",
            headers=_auth(),
            json={
                "id": "parallel-input",
                "prompt": {"text": "parallel-tools"},
                "delivery": "steer",
                "resume": True,
            },
        )
        assert admitted.status_code == 200
        assert controller.started["parallel-tools-midpoint"].wait(timeout=2)

        active = client.get("/api/v1/session/active", headers=_auth())
        assert active.status_code == 200
        runtime = active.json()["data"]["parallel-session"]
        assert runtime["state"] == "running"
        assert runtime["activity"] == "Running tool"

        calls = client.get(
            "/api/v1/session/parallel-session/tool-call",
            headers=_auth(),
        )
        assert calls.status_code == 200
        assert [item["id"] for item in calls.json()["data"][:2]] == [
            "call-first",
            "call-second",
        ]
        assert [item["status"] for item in calls.json()["data"][:2]] == [
            "ok",
            "running",
        ]

        controller.release["parallel-tools"].set()
        for _ in range(200):
            active = client.get("/api/v1/session/active", headers=_auth())
            if "parallel-session" not in active.json()["data"]:
                break
            time.sleep(0.01)
        else:
            pytest.fail("parallel tool session did not settle")


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


def test_two_sessions_run_concurrently_and_steer_fences_old_generation(
    tmp_path: Path,
) -> None:
    controller = FakeRuntimeController()
    app = create_app(
        HttpAppSettings(
            auth_token=TOKEN,
            session_directory=tmp_path / "sessions",
            agent_factory=controller.factory,
            max_active_sessions=4,
        )
    )
    with TestClient(app) as client:
        root = tmp_path / "repo"
        root.mkdir()
        _create_session(client, root, "session-a")
        _create_session(client, root, "session-b")

        first_a = client.post(
            "/api/v1/session/session-a/prompt",
            headers=_auth(),
            json={"prompt": {"text": "a-first"}, "delivery": "steer"},
        )
        first_b = client.post(
            "/api/v1/session/session-b/prompt",
            headers=_auth(),
            json={"prompt": {"text": "b-first"}, "delivery": "steer"},
        )
        assert first_a.status_code == 200
        assert first_b.status_code == 200
        assert controller.started["a-first"].wait(1)
        assert controller.started["b-first"].wait(1)

        active = client.get("/api/v1/session/active", headers=_auth()).json()["data"]
        assert active["session-a"]["state"] == "running"
        assert active["session-a"]["activity"] == "Thinking"
        assert active["session-b"]["state"] == "running"
        assert active["session-b"]["activity"] == "Thinking"
        assert controller.max_parallel_sessions >= 2

        steered = client.post(
            "/api/v1/session/session-a/prompt",
            headers=_auth(),
            json={"prompt": {"text": "a-correction"}, "delivery": "steer"},
        )
        assert steered.status_code == 200
        assert controller.started["a-correction"].wait(1)

        controller.release["a-correction"].set()
        controller.release["b-first"].set()
        waited_a = client.post(
            "/api/v1/session/session-a/wait",
            headers=_auth(),
            params={"timeoutMs": 3000},
        )
        waited_b = client.post(
            "/api/v1/session/session-b/wait",
            headers=_auth(),
            params={"timeoutMs": 3000},
        )
        assert waited_a.json()["data"]["state"] == "idle"
        assert waited_b.json()["data"]["state"] == "idle"

        messages_a = client.get(
            "/api/v1/session/session-a/message",
            headers=_auth(),
            params={"order": "asc"},
        ).json()["data"]
        correction_input_id = steered.json()["data"]["id"]
        correction_message = next(
            message
            for message in messages_a
            if message["type"] == "user"
            and any(
                part.get("type") == "text" and part.get("text") == "a-correction"
                for part in message.get("content", [])
            )
        )
        assert correction_message["inputID"] == correction_input_id
        texts = [
            part["text"]
            for message in messages_a
            for part in message.get("content", [])
            if part["type"] == "text"
        ]
        assert "a-first" in texts
        assert "a-correction" in texts
        assert "done:a-correction" in texts
        assert not any(text == "done:a-first" for text in texts)

        history = client.get(
            "/api/v1/session/session-a/history",
            headers=_auth(),
        ).json()["data"]
        event_types = [event["type"] for event in history]
        assert "session.interrupted" in event_types
        assert event_types.count("session.prompt.promoted") == 2


def test_uploaded_image_survives_admission_and_runtime_persistence(
    tmp_path: Path,
) -> None:
    controller = FakeRuntimeController()
    app = create_app(
        HttpAppSettings(
            auth_token=TOKEN,
            session_directory=tmp_path / "sessions",
            agent_factory=controller.factory,
        )
    )
    with TestClient(app) as client:
        root = tmp_path / "repo"
        root.mkdir()
        _create_session(client, root, "session-image")
        encoded = BytesIO()
        Image.new("RGB", (2, 2)).save(encoded, format="PNG")

        uploaded = client.post(
            "/api/v1/upload",
            headers=_auth(),
            params={"sessionID": "session-image", "purpose": "promptAttachment"},
            files={"file": ("tiny.png", encoded.getvalue(), "image/png")},
        )
        assert uploaded.status_code == 200
        upload = uploaded.json()["data"]
        assert upload["uri"].startswith("yoke-upload://upl_")

        admitted = client.post(
            "/api/v1/session/session-image/prompt",
            headers=_auth(),
            json={
                "id": "inp-image",
                "prompt": {
                    "text": "look at this",
                    "attachments": [
                        {
                            "type": "file",
                            "uri": upload["uri"],
                            "name": "tiny.png",
                            "mime": "image/png",
                        }
                    ],
                },
                "delivery": "steer",
            },
        )
        assert admitted.status_code == 200
        assert admitted.json()["data"]["prompt"]["attachments"][0]["uri"] == upload[
            "uri"
        ]
        assert controller.started["look at this"].wait(1)
        controller.release["look at this"].set()
        waited = client.post(
            "/api/v1/session/session-image/wait",
            headers=_auth(),
            params={"timeoutMs": 3000},
        )
        assert waited.status_code == 200
        assert waited.json()["data"]["state"] == "idle"

        messages = client.get(
            "/api/v1/session/session-image/message",
            headers=_auth(),
            params={"order": "asc"},
        ).json()["data"]
        user = next(item for item in messages if item["type"] == "user")
        assert {part["type"] for part in user["content"]} == {"text", "image"}
        image = next(part for part in user["content"] if part["type"] == "image")
        assert image == {"type": "image", "name": "tiny.png", "uri": None}
        assert str(tmp_path / "sessions" / "uploads") not in str(messages)


def test_queued_uploaded_image_survives_daemon_restart(tmp_path: Path) -> None:
    session_directory = tmp_path / "sessions"
    root = tmp_path / "repo"
    root.mkdir()
    first_controller = FakeRuntimeController()
    first_app = create_app(
        HttpAppSettings(
            auth_token=TOKEN,
            session_directory=session_directory,
            agent_factory=first_controller.factory,
        )
    )

    with TestClient(first_app) as client:
        _create_session(client, root, "session-restart-image")
        encoded = BytesIO()
        Image.new("RGB", (2, 2)).save(encoded, format="PNG")
        uploaded = client.post(
            "/api/v1/upload",
            headers=_auth(),
            params={
                "sessionID": "session-restart-image",
                "purpose": "promptAttachment",
            },
            files={"file": ("restart.png", encoded.getvalue(), "image/png")},
        )
        assert uploaded.status_code == 200
        upload = uploaded.json()["data"]
        body = {
            "id": "inp-restart-image",
            "prompt": {
                "text": "resume image after restart",
                "attachments": [
                    {
                        "type": "file",
                        "uri": upload["uri"],
                        "name": "restart.png",
                        "mime": "image/png",
                    }
                ],
            },
            "delivery": "queue",
            "resume": False,
        }
        admitted = client.post(
            "/api/v1/session/session-restart-image/prompt",
            headers=_auth(),
            json=body,
        )
        assert admitted.status_code == 200
        assert not first_controller.started["resume image after restart"].is_set()

    second_controller = FakeRuntimeController()
    second_app = create_app(
        HttpAppSettings(
            auth_token=TOKEN,
            session_directory=session_directory,
            agent_factory=second_controller.factory,
        )
    )
    with TestClient(second_app) as client:
        repeated = client.post(
            "/api/v1/session/session-restart-image/prompt",
            headers=_auth(),
            json={**body, "resume": True},
        )
        assert repeated.status_code == 200
        assert repeated.json()["data"]["id"] == "inp-restart-image"
        assert second_controller.started["resume image after restart"].wait(1)
        second_controller.release["resume image after restart"].set()
        waited = client.post(
            "/api/v1/session/session-restart-image/wait",
            headers=_auth(),
            params={"timeoutMs": 3000},
        )
        assert waited.status_code == 200
        assert waited.json()["data"]["state"] == "idle"

        messages = client.get(
            "/api/v1/session/session-restart-image/message",
            headers=_auth(),
            params={"order": "asc"},
        ).json()["data"]
        user = next(item for item in messages if item["type"] == "user")
        image = next(part for part in user["content"] if part["type"] == "image")
        assert image == {"type": "image", "name": "restart.png", "uri": None}


def test_queue_attachment_cleanup_waits_for_last_active_reference(
    tmp_path: Path,
) -> None:
    controller = FakeRuntimeController()
    session_directory = tmp_path / "sessions"
    app = create_app(
        HttpAppSettings(
            auth_token=TOKEN,
            session_directory=session_directory,
            agent_factory=controller.factory,
        )
    )
    with TestClient(app) as client:
        root = tmp_path / "repo"
        root.mkdir()
        _create_session(client, root, "session-cleanup-image")
        encoded = BytesIO()
        Image.new("RGB", (2, 2)).save(encoded, format="PNG")
        uploaded = client.post(
            "/api/v1/upload",
            headers=_auth(),
            params={"sessionID": "session-cleanup-image"},
            files={"file": ("shared.png", encoded.getvalue(), "image/png")},
        )
        upload = uploaded.json()["data"]
        upload_id = upload["uri"].removeprefix("yoke-upload://")
        upload_dir = session_directory / "uploads" / upload_id
        attachment = {
            "type": "file",
            "uri": upload["uri"],
            "name": "shared.png",
            "mime": "image/png",
        }
        for input_id in ("inp-shared-a", "inp-shared-b"):
            admitted = client.post(
                "/api/v1/session/session-cleanup-image/prompt",
                headers=_auth(),
                json={
                    "id": input_id,
                    "prompt": {"text": input_id, "attachments": [attachment]},
                    "delivery": "queue",
                    "resume": False,
                },
            )
            assert admitted.status_code == 200
        assert upload_dir.is_dir()

        queue = client.get(
            "/api/v1/session/session-cleanup-image/queue",
            headers=_auth(),
        ).json()["data"]
        removed_first = client.patch(
            "/api/v1/session/session-cleanup-image/queue",
            headers=_auth(),
            json={
                "expectedRevision": queue["revision"],
                "operations": [
                    {"op": "remove", "id": "inp-shared-a"},
                    {"op": "setPaused", "id": "inp-shared-b", "paused": True},
                ],
            },
        )
        assert removed_first.status_code == 200
        assert upload_dir.is_dir()

        updated = client.patch(
            "/api/v1/session/session-cleanup-image/queue",
            headers=_auth(),
            json={
                "expectedRevision": removed_first.json()["data"]["revision"],
                "operations": [
                    {
                        "op": "update",
                        "id": "inp-shared-b",
                        "prompt": {"text": "no image now", "attachments": []},
                    }
                ],
            },
        )
        assert updated.status_code == 200
        assert not upload_dir.exists()

        history = client.get(
            "/api/v1/session/session-cleanup-image/history",
            headers=_auth(),
            params={"limit": 200},
        ).json()["data"]
        event_types = [event["type"] for event in history]
        assert "session.prompt.removed" in event_types
        assert "session.prompt.edited" in event_types


def test_unbound_upload_binds_to_first_admitting_session(tmp_path: Path) -> None:
    controller = FakeRuntimeController()
    app = create_app(
        HttpAppSettings(
            auth_token=TOKEN,
            session_directory=tmp_path / "sessions",
            agent_factory=controller.factory,
        )
    )
    with TestClient(app) as client:
        root = tmp_path / "repo"
        root.mkdir()
        _create_session(client, root, "upload-owner-a")
        _create_session(client, root, "upload-owner-b")
        encoded = BytesIO()
        Image.new("RGB", (2, 2)).save(encoded, format="PNG")
        uploaded = client.post(
            "/api/v1/upload",
            headers=_auth(),
            files={"file": ("owner.png", encoded.getvalue(), "image/png")},
        ).json()["data"]
        prompt = {
            "text": "owner",
            "attachments": [
                {
                    "type": "file",
                    "uri": uploaded["uri"],
                    "name": "owner.png",
                    "mime": "image/png",
                }
            ],
        }
        first = client.post(
            "/api/v1/session/upload-owner-a/prompt",
            headers=_auth(),
            json={"prompt": prompt, "delivery": "queue", "resume": False},
        )
        assert first.status_code == 200
        second = client.post(
            "/api/v1/session/upload-owner-b/prompt",
            headers=_auth(),
            json={"prompt": prompt, "delivery": "queue", "resume": False},
        )
        assert second.status_code == 403
        assert second.json()["error"]["code"] == "attachment_session_mismatch"


def test_upload_metadata_failure_removes_partial_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = UploadService(SessionStore(tmp_path / "sessions"))
    encoded = BytesIO()
    Image.new("RGB", (2, 2)).save(encoded, format="PNG")
    upload = UploadFile(
        file=BytesIO(encoded.getvalue()),
        filename="partial.png",
        headers=Headers({"content-type": "image/png"}),
    )
    original_write_text = Path.write_text

    def fail_metadata(path: Path, *args, **kwargs):  # noqa: ANN002,ANN003,ANN202
        if path.name == "metadata.json":
            raise OSError("disk full")
        return original_write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_metadata)
    with pytest.raises(OSError, match="disk full"):
        asyncio.run(service.create(upload, session_id=None))
    assert not service.directory.exists() or not list(service.directory.iterdir())
