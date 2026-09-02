from __future__ import annotations

# ruff: noqa: D100,D101,D102,D103,S101

from dataclasses import dataclass
from pathlib import Path
from threading import Event
from threading import RLock
import time
from typing import Any
from typing import cast

from fastapi import FastAPI
from fastapi.testclient import TestClient

from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import Message
from yoke.ai.providers.base import ProviderModelInfo
from yoke.ai.providers.usage_context import current_usage_metric_context
from yoke.ai.providers.usage_context import UsageMetricContext
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


class DelayedTitleProvider(SwitchableProvider):
    def __init__(self, title_started: Event, title_release: Event) -> None:
        super().__init__()
        self.title_started = title_started
        self.title_release = title_release

    def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
    ) -> Message:
        del tools
        last = (messages[-1].plain_text_content or "") if messages else ""
        if last.startswith("Create a concise title"):
            self.title_started.set()
            if not self.title_release.wait(2):
                raise TimeoutError("title test gate was not released")
            return Message.assistant("generated title")
        return Message.assistant("turn response")


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


def test_selection_switches_runtime_and_persists_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import yoke.http.services.runtime as runtime_module

    observed: UsageMetricContext | None = None
    original_switch = runtime_module.switch_agent_provider_model

    def capture_context(*args: Any, **kwargs: Any) -> Any:
        nonlocal observed
        observed = current_usage_metric_context()
        return original_switch(*args, **kwargs)

    monkeypatch.setattr(runtime_module, "switch_agent_provider_model", capture_context)
    root = tmp_path / "repo"
    root.mkdir()
    with _client(tmp_path) as client:
        _create_session(client, root)
        event_service = cast(FastAPI, client.app).state.event_service
        original_live = event_service.live
        live_events: list[tuple[str, dict[str, object]]] = []

        def capture_live(event_type, data, **kwargs):
            live_events.append((event_type, data))
            return original_live(event_type, data, **kwargs)

        monkeypatch.setattr(event_service, "live", capture_live)

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
        assert not any(
            event_type == "session.active.changed" and data.get("state") == "running"
            for event_type, data in live_events
        )
    assert observed is not None
    assert observed.surface == "http"
    assert observed.session_id == "session-a"


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


def test_manual_compaction_propagates_http_session_usage_attribution(
    tmp_path: Path,
    monkeypatch,
) -> None:
    import yoke.http.services.runtime as runtime_module

    observed: UsageMetricContext | None = None

    def capture_context(*_args: object, **_kwargs: object) -> None:
        nonlocal observed
        observed = current_usage_metric_context()
        return None

    monkeypatch.setattr(runtime_module, "force_compact_agent", capture_context)
    root = tmp_path / "repo"
    root.mkdir()
    with _client(tmp_path) as client:
        _create_session(client, root)
        renamed = client.patch(
            "/api/v1/session/session-a",
            headers=_auth(),
            json={"title": "Compaction title"},
        )
        assert renamed.status_code == 200
        compact = client.post(
            "/api/v1/session/session-a/compact",
            headers=_auth(),
            json={"reason": "manual"},
        )
        assert compact.status_code == 202
        waited = client.post(
            "/api/v1/session/session-a/wait",
            headers=_auth(),
            params={"timeoutMs": 3000},
        )
        assert waited.status_code == 200

    assert observed is not None
    assert observed.surface == "http"
    assert observed.session_id == "session-a"
    assert observed.session_title == "Compaction title"


def test_title_regeneration_uses_saved_conversation_and_persists(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    app = create_app(
        HttpAppSettings(
            auth_token=TOKEN,
            session_directory=tmp_path / "sessions",
            agent_factory=_factory,
        )
    )
    with TestClient(app) as client:
        _create_session(client, root)
        store = app.state.session_service.store
        record = store.load("session-a")
        store.save(
            "session-a",
            [Message.user("Improve the sidebar menu"), Message.assistant("Done")],
            existing_record=record,
        )

        regenerated = client.post(
            "/api/v1/session/session-a/title/regenerate",
            headers=_auth(),
            json={},
        )

        assert regenerated.status_code == 200
        assert regenerated.json()["data"]["title"] == "summary"
        persisted = client.get(
            "/api/v1/session/session-a",
            headers=_auth(),
        ).json()["data"]
        assert persisted["title"] == "summary"
        history = client.get(
            "/api/v1/session/session-a/history",
            headers=_auth(),
        ).json()["data"]
        assert any(
            item["type"] == "session.updated" and item["data"]["title"] == "summary"
            for item in history
        )


def test_title_generation_propagates_http_session_usage_attribution(
    tmp_path: Path,
) -> None:
    observed: UsageMetricContext | None = None

    class AttributionProvider(SwitchableProvider):
        def complete(
            self,
            messages: list[Message],
            tools: list[dict[str, object]],
        ) -> Message:
            nonlocal observed
            observed = current_usage_metric_context()
            return super().complete(messages, tools)

    def factory(_record: SessionRecord) -> RuntimeAgent:
        return RuntimeAgent(provider=AttributionProvider(), tools=[])

    root = tmp_path / "repo"
    root.mkdir()
    app = create_app(
        HttpAppSettings(
            auth_token=TOKEN,
            session_directory=tmp_path / "sessions",
            agent_factory=factory,
        )
    )
    with TestClient(app) as client:
        _create_session(client, root)
        store = app.state.session_service.store
        record = store.load("session-a")
        store.save(
            "session-a",
            [Message.user("Title this HTTP session"), Message.assistant("Done")],
            existing_record=record,
        )
        regenerated = client.post(
            "/api/v1/session/session-a/title/regenerate",
            headers=_auth(),
            json={},
        )
        assert regenerated.status_code == 200

    assert observed is not None
    assert observed.surface == "http"
    assert observed.session_id == "session-a"
    assert observed.call_kind == "session_title"


def test_first_prompt_automatically_generates_title(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    with _client(tmp_path) as client:
        _create_session(client, root)

        admitted = client.post(
            "/api/v1/session/session-a/prompt",
            headers=_auth(),
            json={"prompt": {"text": "Improve the sidebar menu"}},
        )
        assert admitted.status_code == 200
        waited = client.post(
            "/api/v1/session/session-a/wait",
            headers=_auth(),
            params={"timeoutMs": 3000},
        )
        assert waited.status_code == 200

        deadline = time.monotonic() + 2
        title = None
        while time.monotonic() < deadline:
            title = client.get(
                "/api/v1/session/session-a",
                headers=_auth(),
            ).json()["data"]["title"]
            if title is not None:
                break
            time.sleep(0.01)

        assert title == "summary"
        history = client.get(
            "/api/v1/session/session-a/history",
            headers=_auth(),
        ).json()["data"]
        assert any(
            item["type"] == "session.updated" and item["data"]["title"] == "summary"
            for item in history
        )


def test_first_prompt_does_not_replace_explicit_title(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    with _client(tmp_path) as client:
        created = client.post(
            "/api/v1/session",
            headers=_auth(),
            json={
                "id": "session-a",
                "location": {"directory": str(root)},
                "title": "Keep this title",
                "selection": {
                    "provider": "demo",
                    "model": "gpt-a",
                    "reasoningEffort": "medium",
                },
            },
        )
        assert created.status_code == 200
        admitted = client.post(
            "/api/v1/session/session-a/prompt",
            headers=_auth(),
            json={"prompt": {"text": "Improve the sidebar menu"}},
        )
        assert admitted.status_code == 200
        waited = client.post(
            "/api/v1/session/session-a/wait",
            headers=_auth(),
            params={"timeoutMs": 3000},
        )
        assert waited.status_code == 200
        session = client.get(
            "/api/v1/session/session-a",
            headers=_auth(),
        ).json()["data"]
        assert session["title"] == "Keep this title"


def test_manual_title_cancels_inflight_automatic_title(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    title_started = Event()
    title_release = Event()

    def factory(_record: SessionRecord) -> RuntimeAgent:
        return RuntimeAgent(
            provider=DelayedTitleProvider(title_started, title_release),
            tools=[],
        )

    app = create_app(
        HttpAppSettings(
            auth_token=TOKEN,
            session_directory=tmp_path / "sessions",
            agent_factory=factory,
        )
    )
    with TestClient(app) as client:
        _create_session(client, root)
        admitted = client.post(
            "/api/v1/session/session-a/prompt",
            headers=_auth(),
            json={"prompt": {"text": "Improve the sidebar menu"}},
        )
        assert admitted.status_code == 200
        assert title_started.wait(1)

        renamed = client.patch(
            "/api/v1/session/session-a",
            headers=_auth(),
            json={"title": "Manual title"},
        )
        assert renamed.status_code == 200
        title_release.set()
        waited = client.post(
            "/api/v1/session/session-a/wait",
            headers=_auth(),
            params={"timeoutMs": 3000},
        )
        assert waited.status_code == 200
        time.sleep(0.05)

        session = client.get(
            "/api/v1/session/session-a",
            headers=_auth(),
        ).json()["data"]
        assert session["title"] == "Manual title"
