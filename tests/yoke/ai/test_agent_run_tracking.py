"""SDK admission, identity, attribution and cancellation without model traffic."""

from __future__ import annotations

import asyncio
from contextvars import Context
import json
from pathlib import Path
import sys
import threading

import pytest

from yoke.agent.models import Message
from yoke.agent.models import ToolCall, ToolFunction
from yoke.agent.tools import LocalTool
from yoke.agent.tools.command_process_manager import CommandProcessManager
from yoke.agent_runs import AgentRunRegistry
from yoke.agent_runs.context import Binding, bind_binding
from yoke.agent_runs.protocol import ENVIRONMENT_KEY, RegistrationError
from yoke.ai.providers.usage_context import current_usage_metric_context
from yoke.ai.sdk import Agent, BatchTask, RunConfig, Skill, run_many


class Provider:
    provider_name = "test-provider"
    supports_image_inputs = False
    max_images_per_message = None

    def __init__(self, *, fail: bool = False):
        self.calls = []
        self.fail = fail

    def complete(self, messages, tools):
        self.calls.append(
            (current_usage_metric_context(), [m.model_dump() for m in messages], tools)
        )
        if self.fail:
            raise ValueError("SECRET_ERROR")
        return Message.assistant("SECRET_OUTPUT")


@pytest.fixture
def manager():
    value = CommandProcessManager()
    try:
        yield value
    finally:
        value.close()


def config(root: Path, **kwargs) -> RunConfig:
    return RunConfig(root=root, include_agents_file=False, **kwargs)


def test_reuse_fork_and_load_get_distinct_identities(manager, tmp_path):
    provider = Provider()
    with manager.agent_runs.bind(session_id="owner"):
        agent = Agent(provider=provider, config=config(tmp_path, name="reviewer"))
        assert manager.agent_runs.snapshots() == []
        fork = None
        loaded = None
        try:
            agent.prompt("SECRET_PROMPT")
            agent.prompt("SECRET_FOLLOWUP")
            fork = agent.fork()
            fork.prompt("SECRET_FORK")
            path = tmp_path / "state.json"
            agent.save(path)
            loaded = Agent.load(path, provider=Provider(), config=config(tmp_path))
            loaded.prompt("SECRET_LOADED")
            assert len({agent.agent_id, fork.agent_id, loaded.agent_id}) == 3
            rows = manager.agent_runs.snapshots()
            assert len(rows) == 4
            assert len({row["runId"] for row in rows}) == 4
            assert len([row for row in rows if row["agentId"] == agent.agent_id]) == 2
            assert all(row["status"] == "completed" for row in rows)
            assert all(row["sessionID"] == "owner" for row in rows)
            assert "SECRET" not in json.dumps(rows)
            assert rows[-1]["runId"] == provider.calls[0][0].sdk_run_id
        finally:
            if loaded is not None:
                loaded.close()
            if fork is not None:
                fork.close()
            agent.close()


def test_registration_denial_precedes_provider_call(manager, tmp_path):
    provider = Provider()
    address = manager.agent_runs.address()
    with bind_binding(Binding(token="wrong", address=address)):
        agent = Agent(provider=provider, config=config(tmp_path))
        try:
            with pytest.raises(RegistrationError):
                agent.prompt("SECRET_PROMPT")
            assert provider.calls == []
            assert manager.agent_runs.snapshots() == []
        finally:
            agent.close()


def test_failure_and_usage_opt_out_keep_host_ownership(manager, tmp_path):
    provider = Provider(fail=True)
    with manager.agent_runs.bind(session_id="host-owner"):
        agent = Agent(
            provider=provider,
            config=config(
                tmp_path,
                inherit_usage_attribution=False,
                root_session_id="explicit-usage-owner",
            ),
        )
        try:
            with pytest.raises(ValueError, match="SECRET_ERROR"):
                agent.prompt("SECRET_PROMPT")
            row = manager.agent_runs.snapshots()[0]
            assert row["status"] == "failed" and row["errorType"] == "ValueError"
            assert row["sessionID"] == "host-owner"
            assert provider.calls[0][0].session_id == "explicit-usage-owner"
            assert "SECRET" not in json.dumps(row)
        finally:
            agent.close()


def test_lost_registration_ack_retries_without_duplicate_run(
    manager, tmp_path, monkeypatch
):
    original = Binding.call
    dropped = []

    def call(binding, frame):
        response = original(binding, frame)
        if frame.get("op") == "register" and not dropped:
            dropped.append(True)
            raise RegistrationError("Lost ACK")
        return response

    monkeypatch.setattr(Binding, "call", call)
    provider = Provider()
    with manager.agent_runs.bind(session_id="owner"):
        agent = Agent(provider=provider, config=config(tmp_path))
        try:
            agent.prompt("prompt")
            assert len(provider.calls) == 1
            assert len(manager.agent_runs.snapshots()) == 1
            assert manager.agent_runs.snapshots()[0]["status"] == "completed"
        finally:
            agent.close()


def test_reporting_loss_never_replaces_provider_result(manager, tmp_path):
    class ClosingProvider(Provider):
        def complete(self, messages, tools):
            manager.agent_runs.close()
            return super().complete(messages, tools)

    with manager.agent_runs.bind(session_id="owner"):
        agent = Agent(provider=ClosingProvider(), config=config(tmp_path))
        try:
            assert agent.prompt("prompt").output == "SECRET_OUTPUT"
            row = manager.agent_runs.snapshots()[0]
            assert row["status"] == "running" and row["observation"] == "lost"
        finally:
            agent.close()


def test_observer_errors_do_not_fail_the_run(manager, tmp_path):
    class BadObserver:
        def observe(self, event):
            raise RuntimeError("SECRET_OBSERVER_ERROR")

    with manager.agent_runs.bind(session_id="owner"):
        agent = Agent(
            provider=Provider(), config=config(tmp_path), observer=BadObserver()
        )
        try:
            assert agent.prompt("SECRET_PROMPT").output == "SECRET_OUTPUT"
            assert manager.agent_runs.snapshots()[0]["status"] == "completed"
            assert "SECRET" not in json.dumps(manager.agent_runs.snapshots())
        finally:
            agent.close()


def test_async_agents_register_independently_and_cancel_only_after_drain(
    manager, tmp_path
):
    entered = threading.Event()
    release = threading.Event()

    class BlockingProvider(Provider):
        def complete(self, messages, tools):
            entered.set()
            assert release.wait(5)
            return super().complete(messages, tools)

    async def scenario():
        async with Agent(
            provider=BlockingProvider(), config=config(tmp_path, name="blocked")
        ) as blocked:
            async with Agent(
                provider=Provider(), config=config(tmp_path, name="quick")
            ) as quick:
                task = asyncio.create_task(blocked.prompt_async("blocked"))
                assert await asyncio.to_thread(entered.wait, 5)
                await quick.prompt_async("quick")
                rows = {row["name"]: row for row in manager.agent_runs.snapshots()}
                assert rows["blocked"]["status"] == "running"
                assert rows["quick"]["status"] == "completed"
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert (
                    next(
                        row
                        for row in manager.agent_runs.snapshots()
                        if row["name"] == "blocked"
                    )["status"]
                    == "running"
                )
                release.set()
        rows = {row["name"]: row for row in manager.agent_runs.snapshots()}
        assert rows["blocked"]["status"] == "cancelled"

    try:
        with manager.agent_runs.bind(session_id="owner"):
            asyncio.run(scenario())
    finally:
        release.set()


def test_run_many_retries_share_task_but_not_run_ids(manager, tmp_path):
    providers = []

    def factory(task):
        provider = Provider(fail=not providers)
        providers.append(provider)
        return Agent(provider=provider, config=config(tmp_path))

    with manager.agent_runs.bind(session_id="owner"):
        result = asyncio.run(
            run_many(
                [BatchTask(id="task-a", prompt="SECRET_PROMPT")],
                agent_factory=factory,
                max_attempts=2,
            )
        )
    assert result.completed_count == 1
    rows = manager.agent_runs.snapshots()
    assert {row["taskId"] for row in rows} == {"task-a"}
    assert {row["attempt"] for row in rows} == {1, 2}
    assert {row["status"] for row in rows} == {"failed", "completed"}
    assert {row["runId"] for row in rows} == {
        p.calls[0][0].sdk_run_id for p in providers
    }


def test_tracking_does_not_change_provider_requests_or_usage_opt_out(manager, tmp_path):
    requests = []
    for tracked in (False, True):
        provider = Provider()
        agent = Agent(
            provider=provider,
            config=config(
                tmp_path,
                inherit_usage_attribution=False,
                tools=["file.read", "file.search"],
                skills=[Skill.inline("review", "Check the evidence.")],
                messages=[
                    Message.user("checkpoint"),
                    Message.assistant("saved answer"),
                ],
            ),
        )
        try:
            if tracked:
                with manager.agent_runs.bind(session_id="owner"):
                    agent.prompt("same prompt")
            else:
                agent.prompt("same prompt")
            usage, messages, tools = provider.calls[0]
            assert usage.session_id is None and usage.root_session_id is None
            requests.append((messages, tools))
        finally:
            agent.close()
    assert requests[0] == requests[1]
    assert len(manager.agent_runs.snapshots()) == 1


def test_batch_in_process_tool_prompts_a_distinct_nested_agent(manager, tmp_path):
    child_provider = Provider()

    class NestedTool(LocalTool):
        name = "nested"
        description = "Run a nested SDK prompt."
        execute_in_process = True

        def execute(self):
            child = Agent(
                provider=child_provider, config=config(tmp_path, name="child")
            )
            try:
                return {"ok": True, "output": child.prompt("child prompt").output}
            finally:
                child.close()

    class ParentProvider(Provider):
        def complete(self, messages, tools):
            response = super().complete(messages, tools)
            if len(self.calls) == 1:
                return Message(
                    role="assistant",
                    content=None,
                    tool_calls=[
                        ToolCall(
                            id="nested-call",
                            function=ToolFunction(name="nested", arguments="{}"),
                        )
                    ],
                )
            return response

    parent_provider = ParentProvider()
    with manager.agent_runs.bind(session_id="owner"):
        result = asyncio.run(
            run_many(
                [BatchTask(id="batch-parent", prompt="parent prompt")],
                agent_factory=lambda task: Agent(
                    provider=parent_provider,
                    config=config(
                        tmp_path,
                        name="parent",
                        tools=[NestedTool],
                        tool_execution="sequential",
                    ),
                ),
            )
        )
    assert result.completed_count == 1
    assert len(child_provider.calls) == 1
    rows = {row["name"]: row for row in manager.agent_runs.snapshots()}
    assert rows["child"]["parentRunId"] == rows["parent"]["runId"]
    assert rows["child"]["parentAgentId"] == rows["parent"]["agentId"]
    assert rows["child"]["runId"] != rows["parent"]["runId"]
    assert {row["status"] for row in rows.values()} == {"completed"}
    assert child_provider.calls[0][0].sdk_run_id == rows["child"]["runId"]
    assert parent_provider.calls[0][0].sdk_run_id == rows["parent"]["runId"]


@pytest.mark.parametrize(
    "task_id",
    ["x" * 100_000, "line\nbreak", "path/to/task", "\x00task\t"],
    ids=["long", "newline", "slashes", "controls"],
)
def test_optional_batch_metadata_never_rejects_valid_task_ids(
    manager, tmp_path, task_id
):
    from contextlib import nullcontext

    requests = []
    for tracked in (False, True):
        provider = Provider()
        with manager.agent_runs.bind(session_id="owner") if tracked else nullcontext():
            result = asyncio.run(
                run_many(
                    [BatchTask(id=task_id, prompt="same prompt")],
                    agent_factory=lambda task: Agent(
                        provider=provider, config=config(tmp_path)
                    ),
                )
            )
        assert result.completed_count == 1, result.items[0].error
        assert result.items[0].task.id == task_id
        requests.append(provider.calls[0][1:])
    assert requests[0] == requests[1]
    row = manager.agent_runs.snapshots()[0]
    assert row["attempt"] == 1
    assert isinstance(row["taskId"], str) and 0 < len(row["taskId"]) <= 256
    assert all(ord(char) >= 32 for char in row["taskId"])


@pytest.mark.parametrize("tool_name", ["command_exec", "python_exec"])
def test_commands_survive_reporting_outage_but_nested_sdk_requires_ack(
    manager, tmp_path, tool_name
):
    from yoke.agent.tools.command import ExecCommandTool
    from yoke.agent.tools.python_exec import PythonExecTool

    marker = tmp_path / "ordinary-command"
    provider_marker = tmp_path / "provider-called"
    ordinary = f"from pathlib import Path; Path({str(marker)!r}).write_text('ran'); print('NORMAL_RESULT')"
    nested = f"""
from pathlib import Path
from yoke.ai.sdk import Agent, RunConfig
from yoke.agent_runs.protocol import RegistrationError
class Provider:
    provider_name = 'fixture'
    supports_image_inputs = False
    max_images_per_message = None
    def complete(self, messages, tools):
        Path({str(provider_marker)!r}).write_text('called')
        raise AssertionError('Provider ran without registration')
agent = Agent(provider=Provider(), config=RunConfig(root=Path.cwd(), include_agents_file=False))
try:
    try:
        agent.prompt('must register')
    except RegistrationError:
        print('SDK_ADMISSION_DENIED')
    else:
        raise AssertionError('SDK prompt bypassed admission')
finally:
    agent.close()
"""

    class OutageProvider(Provider):
        def complete(self, messages, tools):
            response = super().complete(messages, tools)
            if len(self.calls) != 1:
                return response
            assert manager.agent_runs.snapshots()[0]["status"] == "running"
            assert manager.agent_runs._server is not None
            manager.agent_runs._server.close()
            return Message(
                role="assistant",
                content=None,
                tool_calls=[
                    ToolCall(
                        id=f"command-{index}",
                        function=ToolFunction(
                            name=tool_name,
                            arguments=json.dumps(
                                {"argv": [sys.executable, "-c", code]}
                                if tool_name == "command_exec"
                                else {"code": code}
                            ),
                        ),
                    )
                    for index, code in enumerate((ordinary, nested))
                ],
            )

    provider = OutageProvider()
    tool = ExecCommandTool if tool_name == "command_exec" else PythonExecTool
    with manager.agent_runs.bind(session_id="owner"):
        agent = Agent(
            provider=provider,
            config=config(tmp_path, tools=[tool], tool_execution="sequential"),
        )
        try:
            assert agent.prompt("run commands").output == "SECRET_OUTPUT"
            assert marker.read_text() == "ran"
            assert not provider_marker.exists()
            assert len(provider.calls) == 2
            messages = provider.calls[-1][1]
            results = [
                json.loads(m["content"]) for m in messages if m["role"] == "tool"
            ]
            assert len(results) == 2
            assert all(result["ok"] and result["exit_code"] == 0 for result in results)
            assert "NORMAL_RESULT" in results[0]["output"]
            assert "SDK_ADMISSION_DENIED" in results[1]["output"]
        finally:
            agent.close()


def test_captured_child_and_forks_outlive_parent_history_and_each_other(
    tmp_path, monkeypatch
):
    monkeypatch.delenv(ENVIRONMENT_KEY, raising=False)
    registry = AgentRunRegistry(history_limit=1)
    children = []
    parent_run = []

    class ParentProvider(Provider):
        def complete(self, messages, tools):
            parent_run.append(current_usage_metric_context().sdk_run_id)
            children.append(Agent(provider=Provider(), config=config(tmp_path)))
            return super().complete(messages, tools)

    parent = None
    fork = None
    try:
        with registry.bind(session_id="owner"):
            parent = Agent(provider=ParentProvider(), config=config(tmp_path))
            parent.prompt("construct child")
        parent.close()
        child = children[0]
        child_host = child._run_host.binding
        assert child_host is not None
        run_ids = set()
        for _ in range(2):
            assert Context().run(child.prompt, "child").output == "SECRET_OUTPUT"
            row = registry.snapshots()[0]
            assert row["parentRunId"] == parent_run[0]
            assert row["parentAgentId"] == parent.agent_id
            assert row["sessionID"] == "owner"
            run_ids.add(row["runId"])
        assert len(run_ids) == 2
        fork = Context().run(child.fork)
        fork_host = fork._run_host.binding
        assert fork_host is not None and fork_host.token != child_host.token
        child.close()
        assert child_host.token not in registry._capabilities
        assert Context().run(fork.prompt, "fork").output == "SECRET_OUTPUT"
        row = registry.snapshots()[0]
        assert row["agentId"] == fork.agent_id != child.agent_id
        assert row["parentRunId"] == parent_run[0]
        assert row["parentAgentId"] == parent.agent_id
        fork.close()
        assert fork_host.token not in registry._capabilities
    finally:
        if fork is not None:
            fork.close()
        for child in children:
            child.close()
        if parent is not None:
            parent.close()
        registry.close()


def test_standalone_construction_and_fork_do_not_open_tracking(tmp_path, monkeypatch):
    monkeypatch.delenv(ENVIRONMENT_KEY, raising=False)

    def forbidden(*args, **kwargs):
        pytest.fail("Standalone construction attempted tracking I/O")

    monkeypatch.setattr(Binding, "retain", forbidden)
    monkeypatch.setattr(Binding, "call", forbidden)
    agent = Context().run(Agent, provider=Provider(), config=config(tmp_path))
    fork = None
    try:
        fork = agent.fork()
        assert agent._run_host.binding is None
        assert fork._run_host.binding is None
    finally:
        if fork is not None:
            fork.close()
        agent.close()


def test_owned_authority_released_even_when_provider_close_fails(manager, tmp_path):
    class BrokenClose(Provider):
        def close(self):
            raise ValueError("close failed")

    with manager.agent_runs.bind(session_id="owner"):
        agent = Agent(provider=BrokenClose(), config=config(tmp_path))
        host = agent._run_host.binding
        assert host is not None
        assert host.token in manager.agent_runs._capabilities
        with pytest.raises(ValueError, match="close failed"):
            agent.close()
        assert agent.closed
        assert host.token not in manager.agent_runs._capabilities


def test_constructor_and_fork_errors_do_not_leak_authority(
    manager, tmp_path, monkeypatch
):
    from yoke.ai.sdk.resources import ProviderLease

    with manager.agent_runs.bind(session_id="owner"):
        before = set(manager.agent_runs._capabilities)
        with pytest.raises(ValueError):
            Agent(provider=Provider(), config=config(tmp_path, tools=["missing-tool"]))
        assert set(manager.agent_runs._capabilities) == before
        agent = Agent(provider=Provider(), config=config(tmp_path))
        before = set(manager.agent_runs._capabilities)

        def fail(*args, **kwargs):
            raise ValueError("fork failed")

        monkeypatch.setattr(ProviderLease, "acquire", fail)
        try:
            with pytest.raises(ValueError, match="fork failed"):
                agent.fork()
            assert set(manager.agent_runs._capabilities) == before
            assert agent.prompt("still usable").output == "SECRET_OUTPUT"
        finally:
            agent.close()
