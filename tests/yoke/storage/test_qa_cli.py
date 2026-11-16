"""SQLite failures keep CLI sessions usable and produce clean diagnostics."""

from __future__ import annotations

# ruff: noqa: S101

import sqlite3
from io import StringIO
from pathlib import Path
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from tests.yoke.cli.support import active_session_for
from tests.yoke.cli.test_cli_model_switching import SwitchableProvider
from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import Message
from yoke.agent.state import conversation_entries_from_messages
from yoke.cli.config import CLIArgs
from yoke.cli.db_app import db_app
from yoke.cli.interactive.model_commands import _switch_model
from yoke.cli.render import build_console
from yoke.cli.runtime import persist_session_state


def test_turn_save_warns_on_sqlite_lock_and_keeps_memory(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    active = active_session_for(tmp_path)
    agent = RuntimeAgent(provider=SwitchableProvider(), tools=[])
    messages = [Message.user("keep this unsaved turn")]
    agent.load_conversation(
        conversation_entries=conversation_entries_from_messages(messages)
    )
    database = active.store.database
    with database.write() as connection:
        connection.execute("PRAGMA busy_timeout=0")
    with sqlite3.connect(database.path, isolation_level=None) as other:
        other.execute("BEGIN IMMEDIATE")
        try:
            persist_session_state(active, agent, messages)
        finally:
            other.execute("ROLLBACK")
    assert "failed to persist yoke session" in capsys.readouterr().err
    assert agent.messages == messages
    assert not active.store.exists(active.id)
    persist_session_state(active, agent, messages)
    assert active.store.load(active.id).messages == messages


@pytest.mark.parametrize("message", ["database or disk is full", "database is locked"])
def test_model_switch_warns_on_sqlite_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
) -> None:
    agent = RuntimeAgent(provider=SwitchableProvider(), tools=[])
    active = active_session_for(tmp_path)
    output = StringIO()

    def fail(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.OperationalError(message)

    monkeypatch.setattr("yoke.cli.runtime.session.save_active_session_metadata", fail)
    assert (
        _switch_model(
            "demo:gpt-b",
            None,
            agent=agent,
            active_session=active,
            messages=[],
            console=build_console(output),
            args=CLIArgs(root=str(tmp_path)),
        )
        == []
    )
    assert cast(Any, agent.provider).current_model_id() == "gpt-b"
    assert "metadata was not saved" in output.getvalue()


def test_turn_save_warns_on_sqlite_full_and_can_retry(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    active = active_session_for(tmp_path)
    agent = RuntimeAgent(provider=SwitchableProvider(), tools=[])
    messages = [Message.user("x" * 200_000)]
    agent.load_conversation(
        conversation_entries=conversation_entries_from_messages(messages)
    )
    with active.store.database.write() as connection:
        pages = connection.execute("PRAGMA page_count").fetchone()[0]
        connection.execute(f"PRAGMA max_page_count={pages}")
    try:
        persist_session_state(active, agent, messages)
        assert "database or disk is full" in capsys.readouterr().err
        assert agent.messages == messages
        assert not active.store.exists(active.id)
    finally:
        with active.store.database.write() as connection:
            connection.execute("PRAGMA max_page_count=1073741823")
    persist_session_state(active, agent, messages)
    assert active.store.load(active.id).messages == messages


def test_db_info_newer_schema_is_a_clean_cli_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "newer.db"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=999")
    monkeypatch.setenv("YOKE_DB_PATH", str(path))
    result = CliRunner().invoke(db_app, ["info"])
    assert result.exit_code == 1
    assert result.exception is not None
    assert isinstance(result.exception, SystemExit)
    assert "schema version 999" in result.output
    assert "Upgrade Yoke" in result.output
    assert "Traceback" not in result.output
