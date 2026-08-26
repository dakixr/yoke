from __future__ import annotations

# ruff: noqa: D100,D101,D102,D103,S101

from dataclasses import dataclass
from pathlib import Path
from threading import RLock

from fastapi.testclient import TestClient

from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import Message
from yoke.ai.providers.base import ProviderModelInfo
from yoke.http.app import HttpAppSettings
from yoke.http.app import create_app
from yoke.session import SessionRecord


TOKEN = "selection-secret"


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


@dataclass
class SwitchableConfig:
    model: str = "gpt-a"
    reasoning_effort: str | None = "medium"


class SwitchableProvider:
    provider_name = "demo"
    supports_image_inputs = False
    max_images_per_message = None

    def __init__(self) -> None:
        self.config = SwitchableConfig()
        self.lock = RLock()

    def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
    ) -> Message:
        del messages, tools
        return Message.assistant("summary")

    def list_models(self) -> list[ProviderModelInfo]:
        return [
            ProviderModelInfo(
                id="gpt-a",
                display_name="GPT A",
                context_window_tokens=2_000,
                thinking_levels=("low", "medium", "high"),
                default_thinking_level="medium",
            ),
            ProviderModelInfo(
                id="gpt-b",
                display_name="GPT B",
                context_window_tokens=2_000,
                thinking_levels=("low", "medium", "high"),
                default_thinking_level="low",
            ),
        ]

    def current_model_id(self) -> str | None:
        return self.config.model

    def current_model_info(self) -> ProviderModelInfo | None:
        return next(
            (item for item in self.list_models() if item.id == self.config.model),
            None,
        )

    def set_model(
        self,
        model_id: str,
        *,
        reasoning_effort: str | None = None,
    ) -> None:
        self.config.model = model_id
        if reasoning_effort is not None:
            self.config.reasoning_effort = reasoning_effort


def _factory(_record: SessionRecord) -> RuntimeAgent:
    return RuntimeAgent(provider=SwitchableProvider(), tools=[])


def _client(tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(
            HttpAppSettings(
                auth_token=TOKEN,
                session_directory=tmp_path / "sessions",
                agent_factory=_factory,
            )
        )
    )


def _create_session(client: TestClient, root: Path) -> None:
    response = client.post(
        "/api/v1/session",
        headers=_auth(),
        json={
            "id": "session-a",
            "location": {"directory": str(root)},
            "selection": {
                "provider": "demo",
                "model": "gpt-a",
                "reasoningEffort": "medium",
            },
        },
    )
    assert response.status_code == 200


def test_selection_switches_runtime_and_persists_metadata(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    with _client(tmp_path) as client:
        _create_session(client, root)

        selected = client.post(
            "/api/v1/session/session-a/selection",
            headers=_auth(),
            json={
                "provider": "demo",
                "model": "gpt-b",
                "reasoningEffort": "high",
            },
        )

        assert selected.status_code == 200
        assert selected.json()["data"] == {
            "effective": {
                "provider": "demo",
                "model": "gpt-b",
                "reasoningEffort": "high",
            },
            "applies": "immediately",
        }
        session = client.get(
            "/api/v1/session/session-a",
            headers=_auth(),
        ).json()["data"]
        assert session["selection"] == {
            "provider": "demo",
            "model": "gpt-b",
            "reasoningEffort": "high",
        }
        history = client.get(
            "/api/v1/session/session-a/history",
            headers=_auth(),
        ).json()["data"]
        assert "session.selection.changed" in [item["type"] for item in history]


def test_manual_compaction_is_scheduled_and_waitable(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    with _client(tmp_path) as client:
        _create_session(client, root)

        compact = client.post(
            "/api/v1/session/session-a/compact",
            headers=_auth(),
            json={"reason": "manual"},
        )
        assert compact.status_code == 202
        assert compact.json()["data"]["accepted"] is True
        assert compact.json()["data"]["operationID"].startswith("op_")

        waited = client.post(
            "/api/v1/session/session-a/wait",
            headers=_auth(),
            params={"timeoutMs": 3000},
        )
        assert waited.status_code == 200
        assert waited.json()["data"]["state"] == "idle"

        history = client.get(
            "/api/v1/session/session-a/history",
            headers=_auth(),
        ).json()["data"]
        event_types = [item["type"] for item in history]
        assert "session.compaction.started" in event_types
        assert "session.compaction.ended" in event_types
