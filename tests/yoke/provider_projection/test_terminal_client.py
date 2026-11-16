"""Terminal turns over a real Unix listener retain the frozen CLI projection."""

from __future__ import annotations

import json
from contextlib import ExitStack

import pytest

from tests.yoke.cli.daemon_support import serve_app
from tests.yoke.cli.support import CaptureStream
from yoke.cli.backend.connection import SessionConnection
from yoke.cli.backend.headless import run_headless_prompt
from yoke.http.app import HttpAppSettings, create_app

from .golden import Normalizer, encode, storage_projection
from .recording import Gate, Tape
from .runtime import World
from .test_oracle import GOLDEN, scenario


class Terminal:
    """Use CLI admission and rendering; issue slash operations through the API."""

    def __init__(self, world: World):
        self.world = world
        self.session_id = "oracle-session"
        self.stack = ExitStack()
        app = create_app(
            HttpAppSettings(
                auth_token=None,
                session_directory=world.directory,
                agent_factory=world.agent,
            )
        )
        handle = self.stack.enter_context(serve_app(app))
        self.connection = SessionConnection(
            self.session_id, discover=lambda: handle, cwd=world.root
        )
        self.stack.callback(self.connection.close)
        self.connection.request(
            "POST",
            "session",
            json={
                "id": self.session_id,
                "title": "Oracle fixture",
                "location": {"directory": str(world.root)},
            },
        )

    def prompt(
        self, text: str, *, image: bool = False, gate: Gate | None = None
    ) -> None:
        assert gate is None
        stdout, stderr = CaptureStream(), CaptureStream()
        assert (
            run_headless_prompt(
                self.connection,
                text,
                image_paths=(self.world.root / "tiny.png",) if image else (),
                stdout=stdout,
                stderr=stderr,
            )
            == 0
        ), stderr.getvalue()

    def compact(self) -> None:
        self.connection.session_request("POST", "/compact", json={"reason": "manual"})
        assert (
            self.connection.session_request(
                "POST", "/wait", params={"timeoutMs": 5000}
            )["data"]["state"]
            == "idle"
        )

    def activate(self) -> None:
        self.connection.session_request("POST", "/skill/oracle/activate", json={})

    def navigate(self, target: str, *, summarize: bool = False) -> None:
        tree = self.connection.session_request("GET", "/tree")["data"]
        self.connection.session_request(
            "POST",
            "/tree/navigate",
            json={
                "targetID": target,
                "expectedRevision": tree["revision"],
                "branchSummary": "Branch handoff." if summarize else None,
            },
        )

    def fork(self) -> None:
        info = self.connection.session_request("POST", "/fork", json={})["data"]
        self.session_id = info["id"]
        self.connection.session_id = info["id"]

    def title(self) -> None:
        assert (
            self.connection.session_request("POST", "/title/regenerate")["data"][
                "title"
            ]
            == "Oracle title"
        )

    def close(self) -> None:
        self.stack.close()


@pytest.mark.parametrize(
    "name",
    ["tools"],
)
def test_terminal_daemon_request_bytes_match_frozen_in_process_projection(
    tmp_path, monkeypatch, name
):
    directory = tmp_path / "sessions"
    monkeypatch.setenv("YOKE_DB_PATH", str(directory / "yoke.db"))
    monkeypatch.setenv("YOKE_SESSION_DIR", str(directory))
    tape = Tape()
    world = World(tmp_path / "workspace", directory, name, tape)
    normalizer = Normalizer(world.root)
    driver = Terminal(world)
    try:
        scenario(driver, name, normalizer)
    finally:
        driver.close()
    expected = json.loads((GOLDEN / f"{name}.in_process.json").read_bytes())["calls"]
    assert encode(storage_projection(normalizer.normalize(tape.calls))) == encode(
        storage_projection(expected)
    )
