"""Differential provider projection across the shared turn execution seam."""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from threading import Event

import pytest

from yoke.agent.context import ContextManager
from yoke.agent.loop.agent import RuntimeAgent
from yoke.agent.models import Message, ToolCall, ToolFunction
from yoke.agent.session_tree import SessionTree
from yoke.agent.skills.models import ActiveSkill
from yoke.agent.state import conversation_entries_from_messages
from yoke.agent.tools import LocalTool
from yoke.cli.interactive import turn_runner
from yoke.cli.interactive.common import TurnSuccess
from yoke.cli.render import InteractiveRenderer
from yoke.cli.runtime import ActiveSession, execute_turn
from yoke.cli.runtime.turn_agent import prepare_turn_agent
from yoke.cli.runtime.workspaces import take_turn_workspace

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
    tmp_path, monkeypatch, scenario
):
    monkeypatch.setattr(
        turn_runner, "start_session_title_generation", lambda *_args, **_kwargs: None
    )
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
    providers = [
        RecordingProvider(use_tool=scenario == "tools_checkpoint") for _ in range(2)
    ]
    agents = [
        RuntimeAgent(
            provider,
            [EchoTool.bind()] if scenario == "tools_checkpoint" else [],
            context_manager=ContextManager(
                instructions=[Message.system("base instructions\n  preserve spacing")]
            ),
            active_skills=active_skills,
            tool_root=tmp_path,
            tool_execution="sequential",
        )
        for provider in providers
    ]
    checkpoint_payloads = [[], []]
    prepared = None
    outcome = None
    try:
        # The established prompt worker prepared an isolated runtime then called
        # execute_turn with exactly these messages, entries and user blocks.
        prepared = prepare_turn_agent(
            agents[0], messages=history, entries=entries, active_session=session
        )
        legacy = execute_turn(
            prepared,
            "current input",
            list(history),
            indicator=InteractiveRenderer(io.StringIO()),
            stop_requested=lambda: False,
            user_message=user_message,
            conversation_entries=entries,
            after_tool_result_appended=lambda messages, _entries: checkpoint_payloads[
                0
            ].append([message.model_dump_json() for message in messages]),
        )
        outcome = turn_runner.TurnRunner(agents[1], session).run(
            "current input",
            list(history),
            entries=entries,
            stop_event=Event(),
            user_message=user_message,
            indicator=InteractiveRenderer(io.StringIO()),
            checkpoint=lambda _agent, messages, _entries: checkpoint_payloads[1].append(
                [message.model_dump_json() for message in messages]
            ),
        )
        assert isinstance(outcome, TurnSuccess)
        assert providers[0].requests == providers[1].requests
        assert [message.model_dump_json() for message in legacy.messages] == [
            message.model_dump_json() for message in outcome.result.messages
        ]
        assert checkpoint_payloads[0] == checkpoint_payloads[1]
        if scenario == "branch":
            serialized = repr(providers[1].requests)
            assert "inactive branch must stay hidden" not in serialized
            assert "selected branch summary" in serialized
        if scenario == "tools_checkpoint":
            assert checkpoint_payloads[0]
    finally:
        turn_agents = [prepared, outcome.agent if outcome is not None else None]
        for agent in turn_agents:
            if isinstance(agent, RuntimeAgent):
                with take_turn_workspace(agent):
                    agent.close()
        for agent in agents:
            agent.close()
