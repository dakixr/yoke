"""Today's file-store provider projections are a contract for later migrations."""

from __future__ import annotations

import base64
import copy
import os
import shutil
from pathlib import Path

import pytest

from yoke.agent.models import (
    Message,
    MessageImageURL,
    MessageImageURLContentPart,
    MessageTextContentPart,
)
from yoke.ai.providers.base import ProviderError
from yoke.session import SessionStore

from .drivers import Daemon, InProcess
from .runtime import Driver, World
from .golden import Normalizer, encode
from .recording import Gate, Tape, answer, tool_calls

SCENARIOS = (
    "chat",
    "tools",
    "parallel_tools",
    "inline_skill",
    "slash_skill",
    "images_supported",
    "images_unsupported",
    "local_images_supported",
    "local_images_unsupported",
    "manual_compaction",
    "automatic_compaction",
    "overflow_retry",
    "checkpoint_stop",
    "tree_navigation",
    "fork",
    "title",
)
GOLDEN = Path(__file__).parent / "golden"


def turn(
    driver: Driver, text: str, response: Message | None = None, *, image: bool = False
) -> None:
    driver.world.tape.expect(response)
    driver.prompt(text, image=image)


def scenario(driver: Driver, name: str, normalizer: Normalizer) -> None:
    tape = driver.world.tape

    def register() -> None:
        normalizer.register(
            SessionStore(driver.world.directory).load(driver.session_id),
            fork=driver.session_id != "oracle-session",
        )

    if name in {"tools", "parallel_tools"}:
        calls: list[tuple[str, dict[str, object]]] = [
            ("rg", {"patterns": ["alpha"], "paths": ["input.txt"], "sort": "path"}),
            ("read", {"path": "input.txt"}),
            ("write", {"path": "output.txt", "content": "written\n"}),
            ("command_exec", {"argv": ["/bin/echo", "oracle-command"], "login": False}),
        ]
        if name == "parallel_tools":
            tape.expect(tool_calls(*calls))
        else:
            for call in calls:
                tape.expect(tool_calls(call))
        tape.expect(answer("Tools complete."))
        driver.prompt("Inspect, write and verify the workspace.")
        assert (driver.world.root / "output.txt").read_text() == "written\n"
    elif name == "inline_skill":
        turn(driver, "Use $oracle, preserve punctuation: a,b!")
        register()
        turn(driver, "Now use $review too.")
    elif name == "slash_skill":
        driver.activate()
        register()
        turn(driver, "Apply the activated instructions.")
    elif "images_" in name:
        if name.endswith("images_unsupported") and isinstance(driver, Daemon):
            driver.prompt(
                "Describe this local image.",
                image=True,
                expected_error="Selected model does not support image inputs",
            )
            assert not tape.calls
            driver.select_images(True)
        response = answer()
        response.content = [
            MessageTextContentPart(text="An image response."),
            MessageImageURLContentPart(
                image_url=MessageImageURL(
                    url="data:image/png;base64,"
                    + base64.b64encode(
                        (driver.world.root / "tiny.png").read_bytes()
                    ).decode("ascii")
                ),
                label="generated image",
            ),
        ]
        turn(driver, "Describe this local image.", response, image=True)
        if name.endswith("images_unsupported") and isinstance(driver, Daemon):
            driver.select_images(False)
        turn(driver, "Recall the image and your response.")
    elif name == "manual_compaction":
        turn(driver, "Remember the exact constraint: keep input.txt.")
        tape.expect(
            answer("Handoff: preserve input.txt; verify beta."),
            kind="compaction_summary",
        )
        driver.compact()
        register()
        turn(driver, "Continue using the handoff.")
    elif name == "automatic_compaction":
        turn(
            driver,
            "Start a long investigation.",
            answer("alpha " * 2200, output_tokens=3300),
        )
        tape.expect(
            answer("Automatic handoff: verify alpha."), kind="compaction_summary"
        )
        tape.expect()
        driver.prompt("Cross the threshold and continue.")
        register()
        turn(driver, "Use the automatic handoff on another turn.")
    elif name == "overflow_retry":
        turn(driver, "Keep the previous decision.")
        tape.expect(
            ProviderError("context_length_exceeded: maximum context length exceeded")
        )
        tape.expect(
            answer("Overflow handoff: keep the decision."), kind="compaction_summary"
        )
        tape.expect(answer("Recovered."), kind="overflow_retry")
        driver.prompt("Recover from a provider overflow.")
        register()
        turn(driver, "Continue after recovery.")
    elif name == "checkpoint_stop":
        gate = Gate()
        tape.expect(tool_calls(("read", {"path": "input.txt"})))
        tape.expect(answer("Cancelled response must not enter history."), gate=gate)
        driver.prompt("Read, then stop before answering.", gate=gate)
        register()
        turn(driver, "Continue from the accepted tool result.")
    elif name == "tree_navigation":
        turn(driver, "Common root.")
        record = SessionStore(driver.world.directory).load(driver.session_id)
        root_leaf = record.leaf_id
        assert root_leaf is not None
        turn(driver, "Original branch.", answer("Original branch answer."))
        record = SessionStore(driver.world.directory).load(driver.session_id)
        original_leaf = record.leaf_id
        assert original_leaf is not None
        register()
        driver.navigate(root_leaf)
        turn(driver, "Alternative branch.", answer("Alternative branch answer."))
        register()
        if isinstance(driver, InProcess):
            tape.expect(answer("Branch handoff."), kind="branch_summary")
        driver.navigate(original_leaf, summarize=True)
        turn(driver, "Continue the selected branch with its summary.")
    elif name == "fork":
        turn(driver, "Parent session decision.")
        register()
        driver.fork()
        normalizer.register(
            SessionStore(driver.world.directory).load(driver.session_id), fork=True
        )
        turn(driver, "Continue in the fork.")
    elif name == "title":
        turn(driver, "A conversation needing a title.")
        tape.expect(answer("Oracle title"), kind="session_title")
        driver.title()
    else:
        assert name == "chat"
        turn(driver, "First request: café, exact spacing.  Two spaces.")
        turn(driver, "Second request\nwith a new line.", answer("Second answer."))
    assert not tape.steps
    register()


@pytest.mark.parametrize("path", ["in_process", "daemon"])
@pytest.mark.parametrize("name", SCENARIOS)
def test_provider_projection_oracle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str, name: str
) -> None:
    directory = tmp_path / "sessions"
    monkeypatch.setenv("YOKE_SESSION_DIR", str(directory))
    tape = Tape()
    world = World(tmp_path / "workspace", directory, name, tape)
    normalizer = Normalizer(world.root)
    driver_type = InProcess if path == "in_process" else Daemon
    driver = driver_type(world)
    try:
        scenario(driver, name, normalizer)
        scenario_end = len(tape.calls)
        snapshot = tmp_path / "snapshot"
        shutil.copytree(directory, snapshot)
        session_id = driver.session_id
        tape.stage = "continuation"
        turn(
            driver,
            "Storage parity probe: preserve every earlier detail.",
            answer("Probe complete."),
        )
        normalizer.register(
            SessionStore(directory).load(session_id), fork=name == "fork"
        )
        live = normalizer.normalize(tape.calls[scenario_end:])
        normalizer_before_resume = copy.deepcopy(normalizer)
    finally:
        driver.close()

    shutil.rmtree(directory)
    shutil.copytree(snapshot, directory)
    resumed_start = len(tape.calls)
    resumed = driver_type(world, session_id, fresh=False)
    try:
        turn(
            resumed,
            "Storage parity probe: preserve every earlier detail.",
            answer("Probe complete."),
        )
        normalizer_before_resume.register(
            SessionStore(directory).load(session_id), fork=name == "fork"
        )
        restored = normalizer_before_resume.normalize(tape.calls[resumed_start:])
    finally:
        resumed.close()
    assert encode(restored) == encode(live), (
        "Fresh file-store runtime changed provider projection"
    )
    actual = encode(
        {
            "scenario": name,
            "path": path,
            "observations": tape.observations,
            "calls": normalizer.normalize(tape.calls[:scenario_end]),
            "live_continuation": live,
            "resumed_continuation": restored,
        }
    )
    fixture = GOLDEN / f"{name}.{path}.json"
    if os.environ.get("YOKE_REGENERATE_PROJECTION_GOLDENS") == "1":
        fixture.write_bytes(actual)
    assert fixture.read_bytes() == actual, (
        f"Provider projection changed: {fixture.name}"
    )
    assert not tape.steps
