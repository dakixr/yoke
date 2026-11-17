"""Native HTTP attachment bytes and promptless turns using the real agent loop."""

from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path
from threading import Event

from fastapi.testclient import TestClient
from PIL import Image

from tests.yoke.http.test_runtime_lifetime_regressions import (
    TOKEN,
    _auth,
    _create_session,
)
from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import (
    Message,
    MessageImageURLContentPart,
)
from yoke.http.app import HttpAppSettings, create_app
from yoke.session import SessionStore


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
            indexed_runtime_seed=indexed,
        )
    )
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
