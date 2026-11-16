from __future__ import annotations


# ruff: noqa: ANN001,D100,D103,S101
from pathlib import Path
from types import SimpleNamespace


from tests.yoke.http.performance_helpers import TOKEN
from yoke.agent.models import ConversationEntry, Message
from yoke.session.repository.models import SessionRecord
from yoke.session.repository.store import SessionStore

from yoke.http.app import HttpAppSettings, create_app
from yoke.http.runtime import factory as runtime_factory


def test_http_agent_factory_leaves_conversation_loading_to_runtime(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class Agent:
        active_skills = []

        def load_conversation(self, **_kwargs) -> None:
            raise AssertionError("HTTP agent factory must not load session history")

    agent = Agent()
    monkeypatch.setattr(
        runtime_factory,
        "build_cli_agent_from_args",
        lambda _args: SimpleNamespace(agent=agent),
    )
    record = SessionRecord(
        id="large",
        root=str(tmp_path),
        conversation_entries=[
            ConversationEntry(kind="user", message=Message.user("hello"))
        ],
    )

    assert runtime_factory.build_http_session_agent(record) is agent


def test_create_app_does_not_run_session_maintenance_before_server_bind(
    tmp_path: Path,
    monkeypatch,
) -> None:
    def fail(*_args, **_kwargs):
        raise AssertionError("create_app must not block on session maintenance")

    monkeypatch.setattr(SessionStore, "maintain_index", fail)
    app = create_app(
        HttpAppSettings(auth_token=TOKEN, session_directory=tmp_path / "sessions")
    )
    assert (
        app.state.session_service.store.directory == (tmp_path / "sessions").resolve()
    )
