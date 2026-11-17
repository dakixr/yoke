"""SQLite failures keep CLI sessions usable and produce clean diagnostics."""

from __future__ import annotations

# ruff: noqa: S101

import sqlite3
from io import StringIO
from pathlib import Path
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from tests.yoke.cli.test_cli_model_switching import SwitchableProvider
from yoke.agent.loop import RuntimeAgent
from yoke.agent.models import Message
from yoke.cli.config import CLIArgs
from yoke.cli.db_app import db_app
from yoke.cli.interactive.model_commands import _switch_model
from yoke.cli.render import build_console
from tests.yoke.cli.terminal_daemon_support import terminal as terminal


def _daemon_turn_save_failure(tmp_path, monkeypatch, terminal, caplog, *, full):
    from yoke.cli.backend.headless import run_headless_prompt
    from yoke.http.runtime.persistence import TurnPersistence

    prompt = "x" * 200_000 if full else "keep this unsaved turn"
    terminal.factory = lambda _record: RuntimeAgent(
        provider=SwitchableProvider(), tools=[]
    )
    terminal.store.save("disk-failure", [], root=tmp_path, title="Disk fixture")
    backend = terminal.backend("disk-failure", root=tmp_path)
    runtime = terminal.app.state.runtime_registry.get_or_start("disk-failure")
    prepared = []
    prepare = runtime.execution.prepare

    def track(*args, **kwargs):
        agent = prepare(*args, **kwargs)
        prepared.append(agent)
        return agent

    monkeypatch.setattr(runtime.execution, "prepare", track)
    persist = TurnPersistence.persist_turn_entries
    failed = False

    def fault(self, *args, **kwargs):
        nonlocal failed
        if failed:
            return persist(self, *args, **kwargs)
        failed = True
        database = self.store.database
        with database.write() as connection:
            connection.execute("PRAGMA busy_timeout=0")
        if full:
            with database.write() as connection:
                # Admission journals may leave reusable pages. Occupy them so
                # the final transcript must allocate beyond the imposed limit.
                free = connection.execute("PRAGMA freelist_count").fetchone()[0]
                connection.execute("CREATE TABLE quota_guard (data BLOB)")
                connection.execute(
                    "INSERT INTO quota_guard VALUES (zeroblob(?))", ((free + 1) * 4096,)
                )
                pages = connection.execute("PRAGMA page_count").fetchone()[0]
                connection.execute(f"PRAGMA max_page_count={pages}")
            try:
                return persist(self, *args, **kwargs)
            finally:
                with database.write() as connection:
                    connection.execute("PRAGMA max_page_count=1073741823")
                    connection.execute("DROP TABLE quota_guard")
        with sqlite3.connect(database.path, isolation_level=None) as other:
            other.execute("BEGIN IMMEDIATE")
            try:
                return persist(self, *args, **kwargs)
            finally:
                other.execute("ROLLBACK")

    monkeypatch.setattr(TurnPersistence, "persist_turn_entries", fault)
    output, errors = StringIO(), StringIO()
    assert (
        run_headless_prompt(backend.connection, prompt, stdout=output, stderr=errors)
        == 1
    )
    assert failed
    assert "Session finalization failed." in errors.getvalue()
    assert ("database or disk is full" if full else "database is locked") in caplog.text
    assert output.getvalue() == ""
    assert str(terminal.store.database_path) in errors.getvalue().replace("\n", "")
    assert ("database or disk is full" if full else "database is locked") in " ".join(
        errors.getvalue().split()
    )
    with runtime.resources.lock:
        primary = runtime.resources.primary_locked()
        assert primary is not prepared[0]
        if primary is not None:
            assert primary.messages == terminal.store.load("disk-failure").messages
    assert prepared[0].messages == [Message.user(prompt), Message.assistant("done")]
    assert terminal.store.load("disk-failure").messages == []
    assert (
        backend.connection.session_request("POST", "/wait", params={"timeoutMs": 5000})[
            "data"
        ]["state"]
        == "error"
    )
    # Replaying the same admission recovers the unsaved input exactly once.
    records = terminal.app.state.pending_input_service.admissions.load(
        "disk-failure"
    ).records
    assert len(records) == 1
    input_id = next(iter(records))
    backend.connection.session_request(
        "POST",
        "/prompt",
        json={
            "id": input_id,
            "clientID": backend.connection.owner,
            "delivery": "queue",
            "prompt": {"text": prompt, "attachments": []},
        },
    )
    assert (
        backend.connection.session_request("POST", "/wait", params={"timeoutMs": 5000})[
            "data"
        ]["state"]
        == "idle"
    )
    assert terminal.store.load("disk-failure").messages == [
        Message.user(prompt),
        Message.assistant("done"),
    ]
    assert (
        len(
            terminal.app.state.pending_input_service.admissions.load(
                "disk-failure"
            ).records
        )
        == 1
    )


def test_turn_save_warns_on_sqlite_lock_and_keeps_memory(
    tmp_path, monkeypatch, terminal, caplog
) -> None:
    _daemon_turn_save_failure(tmp_path, monkeypatch, terminal, caplog, full=False)


@pytest.mark.parametrize("message", ["database or disk is full", "database is locked"])
def test_model_switch_warns_on_sqlite_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
    terminal,
) -> None:
    agent = RuntimeAgent(provider=SwitchableProvider(), tools=[])
    terminal.factory = lambda _record: agent
    backend = terminal.backend("sqlite-metadata", root=tmp_path)
    output = StringIO()

    def fail(*_args: object, **_kwargs: object) -> None:
        raise sqlite3.OperationalError(message)

    monkeypatch.setattr("yoke.session.SessionStore.set_selection", fail)
    assert (
        _switch_model(
            "demo:gpt-b",
            None,
            agent=backend,
            active_session=backend.session,
            messages=[],
            console=build_console(output),
            args=CLIArgs(root=str(tmp_path)),
        )
        == []
    )
    assert cast(Any, agent.provider).current_model_id() == "gpt-b"
    assert "metadata was not saved" in output.getvalue()


def test_turn_save_warns_on_sqlite_full_and_can_retry(
    tmp_path, monkeypatch, terminal, caplog
) -> None:
    _daemon_turn_save_failure(tmp_path, monkeypatch, terminal, caplog, full=True)


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
