"""Differential provider projection across the shared turn execution seam."""

from __future__ import annotations

import json
from pathlib import Path
from dataclasses import dataclass, field

import pytest

from yoke.agent.context import ContextManager
from yoke.agent.loop.agent import RuntimeAgent
from yoke.agent.models import Message, ToolCall, ToolFunction
from yoke.agent.session_tree import SessionTree
from yoke.agent.skills.models import ActiveSkill
from yoke.agent.state import conversation_entries_from_messages, active_branch_entries
from yoke.agent.tools import LocalTool
from yoke.cli.runtime import ActiveSession
from .terminal_daemon_support import terminal as terminal
from yoke.session import SessionStore

from .support import active_session_for


class EchoTool(LocalTool):
    name = "projection_echo"
    description = "Return a fixed checkpoint result."

    def execute(self) -> dict[str, object]:
        return {"output": "checkpoint tool output"}


@dataclass
class RecordingProvider:
    supports_image_inputs = True
    max_images_per_message = None
    requests: list[tuple[list[str], list[dict[str, object]]]] = field(
        default_factory=list
    )
    use_tool: bool = False
    step: int = 0

    def fork_for_turn(self):
        return RecordingProvider(requests=self.requests, use_tool=self.use_tool)

    def complete(self, messages, tools):
        self.requests.append(
            ([message.model_dump_json() for message in messages], tools)
        )
        self.step += 1
        if self.use_tool and self.step == 1:
            return Message(
                role="assistant",
                content=None,
                tool_calls=[
                    ToolCall(
                        id="projection-call",
                        function=ToolFunction(name="projection_echo", arguments="{}"),
                    )
                ],
            )
        return Message.assistant("projection complete")


@pytest.mark.parametrize(
    "scenario", ["history", "tools_checkpoint", "skills", "images", "branch"]
)
def test_shared_turn_runner_preserves_legacy_prompt_provider_projection(
    tmp_path, monkeypatch, scenario, terminal
):
    session = active_session_for(tmp_path)
    history = [Message.user("previous input"), Message.assistant("previous answer")]
    entries = conversation_entries_from_messages(history)
    if scenario == "branch":
        tree = SessionTree.from_messages(history)
        branch_point = tree.current
        assert branch_point is not None
        tree.append_message(Message.user("inactive branch must stay hidden"))
        tree.checkout(branch_point)
        tree.append_branch_summary(Message.assistant("selected branch summary"))
        tree.append_message(Message.user("selected branch"))
        exported = tree.export_for_persistence()
        record = session.store.save(
            session.id,
            history,
            root=tmp_path,
            conversation_entries=list(exported.entries),
            leaf_id=exported.leaf_id,
        )
        session = ActiveSession(session.id, tmp_path, session.store, record)
        entries = session.active_entries()
        history = session.messages()
    user_message = Message.user("current input")
    if scenario == "images":
        user_message = Message.user(
            [
                {"type": "text", "text": "current input"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,aA=="},
                },
            ]
        )
    active_skills = (
        [
            ActiveSkill(
                name="inline",
                description="Projection skill",
                source_path="<inline>",
                content="skill instructions\n  preserve spacing",
            )
        ]
        if scenario == "skills"
        else []
    )
    provider = RecordingProvider(use_tool=scenario == "tools_checkpoint")
    checkpoints = []
    from yoke.http.runtime.execution import TurnExecutor

    checkpoint = TurnExecutor.checkpoint

    def record_checkpoint(self, execution, context):
        checkpoint(self, execution, context)
        record = SessionStore().load(session.id)
        checkpoints.append(
            [
                entry.message.model_dump_json()
                for entry in (
                    active_branch_entries(
                        record.conversation_entries, leaf_id=record.leaf_id
                    )
                    or []
                )
                if entry.message is not None
            ]
        )

    monkeypatch.setattr(TurnExecutor, "checkpoint", record_checkpoint)

    def factory(_record):
        return RuntimeAgent(
            provider,
            [EchoTool.bind()] if scenario == "tools_checkpoint" else [],
            context_manager=ContextManager(
                instructions=[Message.system("base instructions\n  preserve spacing")]
            ),
            active_skills=active_skills,
            tool_root=tmp_path,
            tool_execution="sequential",
        )

    terminal.factory = factory
    # Continue a saved CLI input, including legacy image bytes. Upload validation
    # is covered by the headless parity image cases.
    tree = SessionTree.restore(entries, entries[-1].id)
    if scenario == "images":
        tree.append_message(user_message)
    exported = tree.export_for_persistence()
    SessionStore().save(
        session.id,
        [],
        root=tmp_path,
        title="Projection fixture",
        conversation_entries=list(exported.entries),
        leaf_id=exported.leaf_id,
        active_skills=active_skills,
    )
    backend = terminal.backend(session.id, root=tmp_path)
    backend.connection.session_request(
        "POST",
        "/prompt",
        json={
            "prompt": {
                "text": "" if scenario == "images" else "current input",
                "continuation": scenario == "images",
            }
        },
    )
    status = backend.connection.session_request(
        "POST", "/wait", params={"timeoutMs": 5000}
    )["data"]
    assert status["state"] == "idle", status
    expected = json.loads(
        (
            Path(__file__).parent / "fixtures" / "turn_projection" / f"{scenario}.json"
        ).read_text()
    )
    assert json.loads(json.dumps(provider.requests)) == expected["requests"]
    record = SessionStore().load(session.id)
    instruction = provider.requests[0][0][0]
    stored_messages = [
        instruction,
        *[
            entry.message.model_dump_json()
            for entry in (
                active_branch_entries(
                    record.conversation_entries, leaf_id=record.leaf_id
                )
                or []
            )
            if entry.message is not None
        ],
    ]
    checkpoints = [[instruction, *messages] for messages in checkpoints]
    assert stored_messages == expected["messages"]
    assert checkpoints == expected["checkpoints"]
    assert backend.messages()[-1].text_content() == "projection complete"
    if scenario == "branch":
        serialized = repr(provider.requests)
        assert "inactive branch must stay hidden" not in serialized
        assert "selected branch summary" in serialized
    if scenario == "tools_checkpoint":
        assert checkpoints


@pytest.mark.parametrize(
    "scenario_name",
    [
        "tools",
        "inline_skill",
        "images_supported",
        "checkpoint_stop",
        "manual_compaction",
        "tree_navigation",
    ],
)
@pytest.mark.parametrize("same_model", [False, True])
def test_indexed_model_selection_matches_full_snapshot_provider_projection(
    tmp_path, monkeypatch, scenario_name, same_model
):
    """Compare requests/cache scope with the established full-snapshot selection."""
    from fastapi import FastAPI
    from typing import cast
    from tests.yoke.provider_projection.drivers import Daemon
    from tests.yoke.provider_projection.golden import Normalizer, encode
    from tests.yoke.provider_projection.recording import Tape, answer
    from tests.yoke.provider_projection.runtime import World
    from tests.yoke.provider_projection.test_oracle import scenario

    recordings = []
    for indexed in (False, True):
        directory = tmp_path / ("indexed" if indexed else "full-snapshot")
        monkeypatch.setenv("YOKE_SESSION_DIR", str(directory))
        world = World(
            tmp_path / f"workspace-{indexed}", directory, scenario_name, Tape()
        )
        normalizer = Normalizer(world.root)
        driver = Daemon(world)
        try:
            scenario(driver, scenario_name, normalizer)
            runtime = cast(
                FastAPI, driver.client.app
            ).state.runtime_registry.get_or_start(driver.session_id)
            if not indexed:
                runtime.operations.message_index = None
            driver.request(
                f"/session/{driver.session_id}/selection",
                {
                    "provider": "oracle",
                    "model": "oracle-model" if same_model else "oracle-vision",
                },
            )
            world.tape.expect(answer("Selected-model continuation."))
            driver.prompt("Continue with every accepted instruction and checkpoint.")
            normalizer.register(SessionStore(directory).load(driver.session_id))
            recordings.append(encode(normalizer.normalize(world.tape.calls)))
            assert not world.tape.steps
        finally:
            driver.close()
    assert recordings[0] == recordings[1]
