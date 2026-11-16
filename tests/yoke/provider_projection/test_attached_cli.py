"""Client environment attachment preserves established provider projections."""

from __future__ import annotations

import os

import pytest

from yoke.session import SessionStore

from .drivers import Daemon
from .golden import Normalizer, encode
from .recording import Tape
from .runtime import World
from .test_oracle import scenario, turn


@pytest.mark.parametrize(
    "name",
    [
        "tools",
        "parallel_tools",
        "inline_skill",
        "slash_skill",
        "checkpoint_stop",
        "images_supported",
        "tree_navigation",
        "manual_compaction",
        "automatic_compaction",
    ],
)
def test_cli_attachment_preserves_provider_messages_cache_and_continuation(
    tmp_path, monkeypatch, name
):
    projections = []
    for attached in (False, True):
        directory = tmp_path / ("attached" if attached else "established")
        monkeypatch.setenv("YOKE_SESSION_DIR", str(directory / "sessions"))
        tape = Tape()
        world = World(directory / "workspace", directory / "sessions", name, tape)
        normalizer = Normalizer(world.root)
        driver = Daemon(world)
        try:
            if attached:
                driver.request(
                    f"/session/{driver.session_id}/client",
                    {
                        "owner": "terminal",
                        "kind": "cli",
                        "env": {
                            **os.environ,
                            "SHLVL": "1",
                            "COLUMNS": "80",
                            "LINES": "24",
                            "MY_SHORT_SECRET": "1",
                            "MY_API_KEY": "Handoff: preserve input.txt; verify beta.",
                            "ANOTHER_SECRET": "Automatic handoff: verify alpha.",
                            "ASSISTANT_TOKEN": "Recorded answer.",
                        },
                        "cwd": str(world.root),
                    },
                    method="PUT",
                )
            scenario(driver, name, normalizer)
            turn(driver, "Continue with exactly the same provider context.")
            normalizer.register(SessionStore(world.directory).load(driver.session_id))
            projections.append(encode(normalizer.normalize(tape.calls)))
            assert not tape.steps
        finally:
            driver.close()
    assert projections[0] == projections[1]
