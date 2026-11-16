"""Interactive rendering and control preserve the frozen in-process requests."""

from __future__ import annotations

import json

import pytest

from tests.yoke.cli.interactive_daemon_support import InteractiveDaemon
from tests.yoke.daemon.support import eventually
from tests.yoke.provider_projection.golden import Normalizer, encode, storage_projection
from tests.yoke.provider_projection.recording import Gate, Tape
from tests.yoke.provider_projection.runtime import World
from tests.yoke.provider_projection.test_oracle import GOLDEN, scenario
from yoke.agent.models import Message


class InteractiveProjection:
    def __init__(self, world, mode, monkeypatch):
        self.world = world
        self.session_id = "oracle-session"
        self.ui = InteractiveDaemon(world)
        self.mode = mode
        self.monkeypatch = monkeypatch

    def prompt(
        self, text: str, *, image: bool = False, gate: Gate | None = None
    ) -> None:
        before = len(self.world.tape.calls)

        def drive(ui):
            if image:
                ui.send(f"/image {self.world.root / 'tiny.png'}")
                ui.wait_text("Attached image: tiny.png")
            ui.send(text)
            if gate is not None:
                assert gate.entered.wait(5)
                ui.keys("\x1b\x1b")
                try:
                    ui.wait_text("Stopped current turn.")
                finally:
                    gate.release.set()
                ui.idle()
            else:
                eventually(lambda: len(self.world.tape.calls) > before)
                ui.idle()
                final = Message.model_validate_json(
                    self.world.tape.calls[-1]["response_json"]
                )
                ui.wait_text(final.final_text_content())

        output = self.ui.run(self.mode, drive, self.monkeypatch)
        assert output.count(text) == 1
        assert not self.world.tape.steps

    def compact(self) -> None:
        def drive(ui):
            ui.send("/compact")
            ui.wait_text("context compaction")
            ui.idle()

        self.ui.run(self.mode, drive, self.monkeypatch)
        assert not self.world.tape.steps

    def activate(self) -> None:
        raise AssertionError("This projection test does not activate skills")

    def navigate(self, target: str, *, summarize: bool = False) -> None:
        raise AssertionError("This projection test does not navigate")

    def fork(self) -> None:
        raise AssertionError("This projection test does not fork")

    def title(self) -> None:
        raise AssertionError("This projection test does not generate titles")

    def close(self) -> None:
        self.ui.close()


@pytest.mark.parametrize(
    "mode,name",
    [("prompt", "checkpoint_stop"), ("basic", "images_supported")],
)
def test_interactive_daemon_request_bytes_match_frozen_in_process_projection(
    tmp_path, monkeypatch, mode, name
):
    directory = tmp_path / "sessions"
    monkeypatch.setenv("YOKE_DB_PATH", str(directory / "yoke.db"))
    world = World(tmp_path / "workspace", directory, name, Tape())
    normalizer = Normalizer(world.root)
    driver = InteractiveProjection(world, mode, monkeypatch)
    try:
        scenario(driver, name, normalizer)
    finally:
        driver.close()
    expected = json.loads((GOLDEN / f"{name}.in_process.json").read_bytes())["calls"]
    actual = encode(storage_projection(normalizer.normalize(world.tape.calls)))
    if actual != encode(storage_projection(expected)):
        (tmp_path / "actual.json").write_bytes(actual)
    assert actual == encode(storage_projection(expected))


def test_interactive_steer_request_bytes_match_frozen_checkpoint_projection(
    tmp_path, monkeypatch
):
    from tests.yoke.provider_projection.recording import answer, tool_calls
    from yoke.session import SessionStore

    directory = tmp_path / "sessions"
    monkeypatch.setenv("YOKE_DB_PATH", str(directory / "yoke.db"))
    tape = Tape()
    world = World(tmp_path / "workspace", directory, "checkpoint_stop", tape)
    gate = Gate()
    tape.expect(tool_calls(("read", {"path": "input.txt"})))
    tape.expect(answer("Cancelled response must not enter history."), gate=gate)
    tape.expect()
    normalizer = Normalizer(world.root)
    ui = InteractiveDaemon(world)

    def drive(terminal):
        terminal.send("Read, then stop before answering.")
        assert gate.entered.wait(5)
        terminal.send("Continue from the accepted tool result.")
        try:
            terminal.wait_text("Model steered.")
        finally:
            gate.release.set()
        terminal.wait_text("Recorded answer.")
        terminal.idle()

    try:
        screen = ui.run("prompt", drive, monkeypatch)
        normalizer.register(SessionStore(directory).load(ui.connection.session_id))
    finally:
        gate.release.set()
        ui.close()
    assert screen.count("Read, then stop before answering.") == 1
    assert screen.count("Continue from the accepted tool result.") == 1
    assert screen.count("Recorded answer.") == 1
    assert "Cancelled response" not in screen
    expected = json.loads((GOLDEN / "checkpoint_stop.in_process.json").read_bytes())[
        "calls"
    ]
    assert encode(storage_projection(normalizer.normalize(tape.calls))) == encode(
        storage_projection(expected)
    )
