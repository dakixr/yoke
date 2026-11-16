"""Admission attachments determine provider accounting and session provenance."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.yoke.ai.test_usage_attribution import UsageProvider
from yoke.agent.loop.agent import RuntimeAgent
from yoke.cli.backend.startup import run_daemon_cli
from yoke.cli.config import CLIArgs

from .support import CaptureStream
from .terminal_daemon_support import TerminalDaemon, terminal as terminal


@pytest.mark.parametrize("kind", ["cli", "web", "acp"])
def test_attachment_attributes_usage_rows_and_session_origin(
    tmp_path: Path,
    terminal: TerminalDaemon,
    kind: str,
) -> None:
    terminal.factory = lambda record: RuntimeAgent(
        UsageProvider(), [], tool_root=Path(record.root)
    )
    backend = terminal.backend("transport", root=tmp_path)
    connection = backend.connection
    connection.request(
        "POST",
        "session",
        json={
            "id": "created",
            "title": "Fixture",
            "location": {"directory": str(tmp_path)},
            "client": {
                "owner": "admitter",
                "kind": kind,
                "mode": "full-access",
                "env": {},
            },
        },
    )
    connection.request(
        "POST",
        "session/created/prompt",
        json={
            "id": "input",
            "clientID": "admitter",
            "resume": False,
            "prompt": {"text": "hello"},
        },
    )
    # Later clients must not steal attribution from the admitted input.
    connection.request(
        "PUT",
        "session/created/client",
        json={
            "owner": "later",
            "kind": "web" if kind != "web" else "cli",
            "env": {},
        },
    )
    connection.request("DELETE", "session/created/client", params={"owner": "admitter"})
    connection.request(
        "PATCH", "session/created/queue", json={"expectedRevision": 1, "operations": []}
    )
    connection.request("POST", "session/created/wait", params={"timeoutMs": 5000})
    with terminal.store.database.read() as db:
        assert db.execute(
            "SELECT origin FROM sessions WHERE id = 'created'"
        ).fetchone() == (kind,)
        assert db.execute(
            "SELECT surface, session_id FROM usage_events WHERE session_id = 'created'"
        ).fetchall() == [(kind, "created")]
    assert terminal.store.load("created").origin == kind


def test_cli_entrypoint_records_cli_origin_and_usage_rows(
    tmp_path: Path, terminal: TerminalDaemon
) -> None:
    terminal.factory = lambda record: RuntimeAgent(
        UsageProvider(), [], tool_root=Path(record.root)
    )
    assert (
        run_daemon_cli(
            CLIArgs(
                root=str(tmp_path), session="cli-created", prompt="hello", headless=True
            ),
            stdout=CaptureStream(),
            stderr=CaptureStream(),
        )
        == 0
    )
    with terminal.store.database.read() as db:
        assert db.execute(
            "SELECT origin FROM sessions WHERE id = 'cli-created'"
        ).fetchone() == ("cli",)
        surfaces = db.execute(
            "SELECT surface FROM usage_events WHERE session_id = 'cli-created'"
        ).fetchall()
    assert surfaces
    assert set(surfaces) == {("cli",)}
