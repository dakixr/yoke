"""Native HTTP attachment bytes and promptless turns using the real agent loop."""

from __future__ import annotations

import base64
import json
from io import BytesIO
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import Message, MessageImageURLContentPart
from yoke.agent.models import ToolCall, ToolFunction
from yoke.agent.tools.read import ReadTool
from yoke.http.app import HttpAppSettings, create_app
from yoke.session import SessionStore
from tests.yoke.http.test_runtime_lifetime_regressions import (
    TOKEN,
    _auth,
    _create_session,
)


class RecordingProvider:
    supports_image_inputs = True

    def __init__(self) -> None:
        self.calls: list[list[Message]] = []
        self.started = Event()
        self.release = Event()
        self.release.set()

    def complete(self, messages, tools):
        self.calls.append([message.model_copy(deep=True) for message in messages])
        self.started.set()
        assert self.release.wait(5), "test did not release provider"
        return Message.assistant(f"answer-{len(self.calls)}", phase="final_answer")


def app_for(root: Path, provider: RecordingProvider, *, indexed: bool = True):
    app = create_app(
        HttpAppSettings(
            auth_token=TOKEN,
            session_directory=root / "sessions",
            agent_factory=lambda _record: RuntimeAgent(provider, []),
            max_active_sessions=1,
        )
    )
    app.state.runtime_registry.indexed_runtime_seed = indexed
    return app


def submit(client: TestClient, input_id: str, prompt: dict, *, resume: bool = True):
    return client.post(
        "/api/v1/session/session-a/prompt",
        headers=_auth(),
        json={
            "id": input_id,
            "prompt": prompt,
            "delivery": "queue",
            "resume": resume,
        },
    )


def drain(client: TestClient, timeout: int = 3000) -> dict:
    response = client.post(
        "/api/v1/session/session-a/drain",
        headers=_auth(),
        params={"timeoutMs": timeout},
    )
    assert response.status_code == 200
    return response.json()["data"]


def messages(client: TestClient) -> list[dict]:
    return client.get(
        "/api/v1/session/session-a/message", headers=_auth(), params={"order": "asc"}
    ).json()["data"]


def png() -> bytes:
    output = BytesIO()
    Image.new("RGB", (2, 2), "red").save(output, format="PNG")
    return output.getvalue()


def cmyk_tiff() -> bytes:
    output = BytesIO()
    Image.new("CMYK", (2, 2), (0, 64, 128, 0)).save(output, format="TIFF")
    return output.getvalue()


@pytest.mark.parametrize("indexed", [False, True])
@pytest.mark.parametrize("promoted", [False, True])
def test_continuation_queue_restart_identity_and_normal_default(
    tmp_path: Path, indexed: bool, promoted: bool
) -> None:
    first = RecordingProvider()
    app = app_for(tmp_path, first, indexed=indexed)
    with TestClient(app) as client:
        _create_session(client, tmp_path, "session-a")
        assert submit(client, "empty", {"continuation": True}).status_code == 400
        assert submit(client, "initial", {"text": "  exact\n"}).status_code == 200
        assert drain(client)["drained"]
        prior = messages(client)
        assert prior[0]["inputID"] == "initial"
        assert first.calls[0][-1].content == "  exact\n"
        assert (
            submit(client, "next", {"continuation": True}, resume=False).status_code
            == 200
        )
        queue = client.get("/api/v1/session/session-a/queue", headers=_auth()).json()[
            "data"
        ]
        assert queue["items"][0]["prompt"]["continuation"] is True
        if promoted:
            assert app.state.pending_input_service.pop_next(
                "session-a", allow_queue=True
            ).continuation
    second = RecordingProvider()
    with TestClient(app_for(tmp_path, second, indexed=indexed)) as client:
        assert submit(client, "next", {"continuation": True}).status_code == 200
        assert drain(client)["drained"]
        after = messages(client)
        assert after[: len(prior)] == prior
        assert [row for row in after if row["type"] == "user"] == [prior[0]]
        assert after[-1]["inputID"] == "next"
        assert after[-1]["content"][0]["text"] == "answer-1"
        assert len(second.calls) == 1
        assert [m.content for m in second.calls[0] if m.role == "user"] == ["  exact\n"]
        # Native admission remains idempotent; an identity conflict is rejected.
        assert submit(client, "next", {"continuation": True}).status_code == 200
        assert drain(client)["drained"]
        assert len(second.calls) == 1
        assert submit(client, "next", {"text": "different"}).status_code == 409
        assert submit(client, "normal", {"text": "normal"}).status_code == 200
        assert drain(client)["drained"]
        assert len([row for row in messages(client) if row["type"] == "user"]) == 2


def test_continuation_interrupt_drain_and_resume_does_not_retag_user(
    tmp_path: Path,
) -> None:
    provider = RecordingProvider()
    with TestClient(app_for(tmp_path, provider)) as client:
        _create_session(client, tmp_path, "session-a")
        assert submit(client, "initial", {"text": "original"}).status_code == 200
        assert drain(client)["drained"]
        prior = messages(client)
        provider.started.clear()
        provider.release.clear()
        try:
            assert (
                submit(client, "cancelled", {"continuation": True}).status_code == 200
            )
            assert provider.started.wait(3)
            assert (
                client.post(
                    "/api/v1/session/session-a/interrupt", headers=_auth()
                ).status_code
                == 200
            )
            assert drain(client, 1) == {"drained": False, "activeWorkers": 1}
            assert messages(client)[: len(prior)] == prior
        finally:
            provider.release.set()
        assert drain(client)["drained"]
        assert submit(client, "resumed", {"continuation": True}).status_code == 200
        assert drain(client)["drained"]
        rows = messages(client)
        assert [row for row in rows if row["type"] == "user"] == [prior[0]]
        assert rows[-1]["inputID"] == "resumed"
        assert rows[-1]["content"][0]["text"] == "answer-3"


@pytest.mark.parametrize(
    "prompt",
    [
        {},
        {"text": " \n"},
        {"text": "x", "continuation": True},
        {"text": " ", "continuation": True},
    ],
)
def test_empty_and_mixed_continuations_are_rejected(
    tmp_path: Path, prompt: dict
) -> None:
    provider = RecordingProvider()
    with TestClient(app_for(tmp_path, provider)) as client:
        _create_session(client, tmp_path, "session-a")
        assert submit(client, "invalid", prompt).status_code == 400
        assert not provider.calls


def test_native_image_and_file_bytes_persist_and_reload(tmp_path: Path) -> None:
    provider = RecordingProvider()
    app = app_for(tmp_path, provider)
    raw_image, raw_file = png(), b"PRIVATE FILE CONTENT\x00\xff"
    with TestClient(app) as client:
        _create_session(client, tmp_path, "session-a")
        attachments = []
        for name, data, mime in [
            ("photo.png", raw_image, "image/png"),
            ("../../metadata.json", raw_file, "application/octet-stream"),
        ]:
            response = client.post(
                "/api/v1/upload",
                headers=_auth(),
                params={"sessionID": "session-a"},
                files={"file": (name, data, mime)},
            )
            assert response.status_code == 200, response.text
            uploaded = response.json()["data"]
            path = app.state.upload_service.resolve(
                uploaded["uri"],
                session_id="session-a",
                name=uploaded["name"],
                mime=mime,
            )
            assert path.read_bytes() == data
            assert path.is_relative_to(tmp_path / "sessions" / "uploads")
            attachments.append({key: uploaded[key] for key in ("uri", "name", "mime")})
        assert (
            submit(client, "attached", {"attachments": attachments}).status_code == 200
        )
        assert drain(client)["drained"]
        row = messages(client)[0]
        image = next(block for block in row["content"] if block["type"] == "image")
        assert image["name"] == "photo.png"
        assert base64.b64decode(image["uri"].split(",", 1)[1]) == raw_image
        assert "PRIVATE FILE CONTENT" not in str(row)
        assert "metadata.json" in str(row)
        before = SessionStore(tmp_path / "sessions").load("session-a")
        assert before is not None
    restored = RecordingProvider()
    with TestClient(app_for(tmp_path, restored)) as client:
        assert messages(client)[0] == row
        assert submit(client, "continued", {"continuation": True}).status_code == 200
        assert drain(client)["drained"]
        parts = [
            part
            for msg in restored.calls[0]
            if isinstance(msg.content, list)
            for part in msg.content
        ]
        image_part = next(
            part for part in parts if isinstance(part, MessageImageURLContentPart)
        )
        assert image_part.attachment_name == "photo.png"
        assert base64.b64decode(image_part.image_url.url.split(",", 1)[1]) == raw_image
        assert "PRIVATE FILE CONTENT" not in str(restored.calls)


def test_valid_cmyk_tiff_is_normalized_before_provider_projection(
    tmp_path: Path,
) -> None:
    provider = RecordingProvider()
    with TestClient(app_for(tmp_path, provider)) as client:
        _create_session(client, tmp_path, "session-a")
        uploaded = client.post(
            "/api/v1/upload",
            headers=_auth(),
            params={"sessionID": "session-a"},
            files={"file": ("photo.tiff", cmyk_tiff(), "image/tiff")},
        )
        assert uploaded.status_code == 200, uploaded.text
        value = uploaded.json()["data"]
        attachment = {key: value[key] for key in ("uri", "name", "mime")}

        assert submit(client, "tiff", {"attachments": [attachment]}).status_code == 200
        assert drain(client)["drained"]
        assert len(provider.calls) == 1
        image_parts = [
            part
            for message in provider.calls[0]
            if isinstance(message.content, list)
            for part in message.content
            if isinstance(part, MessageImageURLContentPart)
        ]
        assert len(image_parts) == 1
        assert image_parts[0].image_url.url.startswith("data:image/png;base64,")


@pytest.mark.parametrize(
    ("name", "data", "mime"),
    [
        ("bad.png", b"not an image", "image/png"),
        ("lie.jpeg", png(), "image/jpeg"),
        ("vector.svg", b"<svg/>", "image/svg+xml"),
    ],
)
def test_upload_rejects_invalid_images_without_leaving_payload(
    tmp_path: Path, name: str, data: bytes, mime: str
) -> None:
    with TestClient(app_for(tmp_path, RecordingProvider())) as client:
        response = client.post(
            "/api/v1/upload", headers=_auth(), files={"file": (name, data, mime)}
        )
        assert response.status_code == 400
    directory = tmp_path / "sessions" / "uploads"
    assert not directory.exists() or not list(directory.iterdir())


def test_file_is_read_by_native_tool_and_continuation_checkpoint_recovers(
    tmp_path: Path,
) -> None:
    class ToolProvider(RecordingProvider):
        def complete(self, messages, tools):
            self.calls.append([message.model_copy(deep=True) for message in messages])
            if len(self.calls) == 1:
                return Message.assistant("initial answer")
            if len(self.calls) == 2:
                user = next(message for message in messages if message.role == "user")
                assert "secret document content" not in str(messages)
                references = json.loads(
                    user.display_text_content().split("tools):\n")[1]
                )
                return Message(
                    role="assistant",
                    tool_calls=[
                        ToolCall(
                            id="read-file",
                            function=ToolFunction(
                                name="read",
                                arguments=json.dumps({"path": references[0]["path"]}),
                            ),
                        )
                    ],
                )
            assert messages[-1].role == "tool"
            assert "secret document content" in messages[-1].plain_text_content
            return Message.assistant("read the file")

    provider = ToolProvider()
    app = create_app(
        HttpAppSettings(
            auth_token=TOKEN,
            session_directory=tmp_path / "sessions",
            agent_factory=lambda _record: RuntimeAgent(
                provider, [ReadTool.bind(root=tmp_path)]
            ),
        )
    )
    with TestClient(app) as client:
        _create_session(client, tmp_path, "session-a")
        upload = client.post(
            "/api/v1/upload",
            headers=_auth(),
            params={"sessionID": "session-a"},
            files={"file": ("document.txt", b"secret document content", "text/plain")},
        ).json()["data"]
        attachment = {key: upload[key] for key in ("uri", "name", "mime")}
        assert submit(client, "file", {"attachments": [attachment]}).status_code == 200
        assert drain(client)["drained"]
        prior = messages(client)
        assert (
            submit(client, "tool-continuation", {"continuation": True}).status_code
            == 200
        )
        assert drain(client)["drained"]
        rows = messages(client)
        assert rows[: len(prior)] == prior
        assert rows[-1]["inputID"] == "tool-continuation", client.get(
            "/api/v1/session/session-a/history", headers=_auth()
        ).json()
        assert rows[-1]["content"][0]["text"] == "read the file"
        # Simulate a crash after final persistence but before settlement. Recovery
        # must recognize this continuation's final assistant, not rerun it.
        admissions = app.state.pending_input_service.admissions
        snapshot = admissions.load("session-a")
        snapshot.records["tool-continuation"].settled = False
        admissions.save("session-a", snapshot)
    restarted = RecordingProvider()
    with TestClient(app_for(tmp_path, restarted)) as client:
        assert (
            submit(client, "tool-continuation", {"continuation": True}).status_code
            == 200
        )
        assert drain(client)["drained"]
        assert not restarted.calls
        assert messages(client) == rows


def test_native_rechecks_image_capability_before_provider_call(tmp_path: Path) -> None:
    provider = RecordingProvider()
    provider.supports_image_inputs = False
    with TestClient(app_for(tmp_path, provider)) as client:
        _create_session(client, tmp_path, "session-a")
        upload = client.post(
            "/api/v1/upload",
            headers=_auth(),
            files={"file": ("image.png", png(), "image/png")},
        ).json()["data"]
        attachment = {key: upload[key] for key in ("uri", "name", "mime")}
        assert submit(client, "image", {"attachments": [attachment]}).status_code == 200
        assert drain(client)["drained"]
        assert not provider.calls
        history = client.get(
            "/api/v1/session/session-a/history", headers=_auth()
        ).json()["data"]
        assert any(event["type"] == "session.runtime.failed" for event in history)


def test_native_upload_enforces_size_and_safe_names(tmp_path: Path) -> None:
    with TestClient(app_for(tmp_path, RecordingProvider())) as client:
        oversized = client.post(
            "/api/v1/upload",
            headers=_auth(),
            files={
                "file": (
                    "large.bin",
                    b"x" * (20 * 1024 * 1024 + 1),
                    "application/octet-stream",
                )
            },
        )
        assert oversized.status_code == 413
        for name in ("..\\..\\metadata.json", "../..", "name\r\nheader.txt", "é" * 200):
            response = client.post(
                "/api/v1/upload",
                headers=_auth(),
                files={"file": (name, b"real bytes", "application/octet-stream")},
            )
            assert response.status_code == 200, response.text
            value = response.json()["data"]
            assert len(value["name"].encode()) <= 128
            assert all(c not in value["name"] for c in "/\\\r\n")


def test_queued_continuation_edit_and_prestart_interrupt(tmp_path: Path) -> None:
    provider = RecordingProvider()
    app = app_for(tmp_path, provider)
    with TestClient(app) as client:
        _create_session(client, tmp_path, "session-a")
        assert submit(client, "initial", {"text": "prior"}).status_code == 200
        assert drain(client)["drained"]
        prior = messages(client)
        assert client.portal is not None
        client.portal.call(app.state.runtime_registry.active_slots.acquire)
        assert (
            submit(client, "queued", {"text": "replace me"}, resume=False).status_code
            == 200
        )
        path = "/api/v1/session/session-a/queue"
        revision = client.get(path, headers=_auth()).json()["data"]["revision"]
        edited = client.patch(
            path,
            headers=_auth(),
            json={
                "expectedRevision": revision,
                "operations": [
                    {"op": "update", "id": "queued", "prompt": {"continuation": True}}
                ],
            },
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["data"]["items"][0]["prompt"]["continuation"] is True

        async def interrupt_before_worker() -> None:
            registry = app.state.runtime_registry
            await registry.wake("session-a")
            runtime = registry.get_if_loaded("session-a")
            assert not runtime._active.worker_started
            await runtime.interrupt()
            assert await runtime.workers.drain(3) == 0
            registry.active_slots.release()

        assert client.portal is not None
        client.portal.call(interrupt_before_worker)
        rows = messages(client)
        assert rows[: len(prior)] == prior
        assert len([row for row in rows if row["type"] == "user"]) == 1
        assert rows[-1]["inputID"] == "queued"
        assert len(provider.calls) == 1
        assert submit(client, "next", {"continuation": True}).status_code == 200
        assert drain(client)["drained"]
        assert messages(client)[-1]["inputID"] == "next"
