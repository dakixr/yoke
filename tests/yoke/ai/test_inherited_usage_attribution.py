"""Accounting lineage must survive tools without changing provider inputs."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shlex
import sys

import pytest

from yoke.agent.loop.in_process_tool import InProcessToolInvocation
from yoke.agent.tools.command import ExecCommandTool
from yoke.agent.tools.command_process_manager import CommandProcessManager
from yoke.agent.tools.python_exec import PythonExecTool
from yoke.ai.providers.usage_attribution import attribute_subprocess_environment
from yoke.ai.providers.usage_context import usage_metric_context
from yoke.ai.sdk import Agent, BatchTask, RunConfig, complete, run_many
from tests.yoke.ai.test_usage_attribution import UsageProvider, read_records


@pytest.mark.parametrize("mode", ["python", "argv", "shell"])
def test_tool_worker_exports_owner_without_mutating_environment(
    tmp_path: Path, monkeypatch, mode: str
) -> None:
    monkeypatch.setenv("YOKE_ROOT_SESSION_ID", "stale")
    monkeypatch.setenv("YOKE_PARENT_RUN_ID", "stale-run")
    code = (
        "import os,json; print(json.dumps([os.getenv('YOKE_ROOT_SESSION_ID'),"
        "os.getenv('YOKE_PARENT_RUN_ID')]))"
    )

    def launch(owner: str) -> list[str]:
        manager = CommandProcessManager()
        cls = PythonExecTool if mode == "python" else ExecCommandTool
        tool = cls.bind(root=tmp_path, command_process_manager=manager)
        args: dict[str, object] = (
            {"code": code}
            if mode == "python"
            else {"argv": [sys.executable, "-c", code]}
            if mode == "argv"
            else {"cmd": shlex.join([sys.executable, "-c", code]), "login": False}
        )
        try:
            with usage_metric_context(surface="cli", session_id=owner):
                invocation = InProcessToolInvocation(
                    tools={tool.name: tool}, name=tool.name, arguments=args
                )
                invocation.start()
                result = invocation.result()
            assert result["ok"]
            return json.loads(str(result["output"]))
        finally:
            manager.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(launch, ["one", "two"])) == [["one", None], ["two", None]]
    assert os.environ["YOKE_ROOT_SESSION_ID"] == "stale"
    assert os.environ["YOKE_PARENT_RUN_ID"] == "stale-run"


def test_agent_snapshots_lineage_and_fork_preserves_it(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("YOKE_USAGE_METRIC_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("YOKE_ROOT_SESSION_ID", "session")
    monkeypatch.setenv("YOKE_PARENT_RUN_ID", "parent")
    agent = Agent(provider=UsageProvider(), config=RunConfig(root=tmp_path))
    monkeypatch.setenv("YOKE_ROOT_SESSION_ID", "other")
    fork = agent.fork()
    try:
        agent.prompt("hello")
        fork.prompt("hello")
    finally:
        fork.close()
        agent.close()
    records = read_records(tmp_path)
    assert len(records) == 2
    for record in records:
        assert record["session_id"] == record["root_session_id"] == "session"
        assert record["parent_run_id"] == "parent"
        assert record["sdk_run_id"] != "parent"


@pytest.mark.parametrize("detach", [False, True])
def test_batch_respects_explicit_config(
    tmp_path: Path, monkeypatch, detach: bool
) -> None:
    monkeypatch.setenv("YOKE_USAGE_METRIC_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("YOKE_ROOT_SESSION_ID", "inherited")
    monkeypatch.setenv("YOKE_PARENT_RUN_ID", "inherited-parent")

    async def run() -> None:
        await run_many(
            [BatchTask(id="a", prompt="hello"), BatchTask(id="b", prompt="hello")],
            agent_factory=lambda _: Agent(
                provider=UsageProvider(),
                config=RunConfig(
                    root=tmp_path,
                    root_session_id=None if detach else "explicit",
                    parent_run_id=None if detach else "explicit-parent",
                    inherit_usage_attribution=not detach,
                ),
            ),
        )

    asyncio.run(run())
    records = read_records(tmp_path)
    assert len({r["sdk_run_id"] for r in records}) == 2
    for record in records:
        assert record["sdk_operation"] == "run_many"
        assert record.get("root_session_id") == (None if detach else "explicit")
        assert record.get("parent_run_id") == (None if detach else "explicit-parent")


def test_nested_lineage_and_detached_environment() -> None:
    with usage_metric_context(
        surface="sdk",
        root_session_id="root",
        sdk_run_id="child",
        parent_run_id="parent",
    ):
        env: dict[str, str] = {}
        attribute_subprocess_environment(env)
        assert env == {"YOKE_ROOT_SESSION_ID": "root", "YOKE_PARENT_RUN_ID": "child"}
    with usage_metric_context(surface="sdk", sdk_run_id="detached"):
        attribute_subprocess_environment(env)
        assert env == {"YOKE_PARENT_RUN_ID": "detached"}


def test_subprocess_sdk_records_parent_session(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("YOKE_USAGE_METRIC_LOG_DIR", str(tmp_path))
    manager = CommandProcessManager()
    tool = PythonExecTool.bind(root=tmp_path, command_process_manager=manager)
    code = """
from yoke.ai import complete
from yoke.agent.models import Message, TokenUsage
class Provider:
    provider_name = 'test-provider'
    supports_image_inputs = False
    max_images_per_message = None
    def complete(self, messages, tools):
        result = Message.assistant('done')
        result.usage = TokenUsage(input_tokens=10, output_tokens=2, total_tokens=12)
        return result
complete('hello', provider=Provider())
"""
    try:
        with usage_metric_context(
            surface="sdk", root_session_id="root", sdk_run_id="parent"
        ):
            invocation = InProcessToolInvocation(
                tools={tool.name: tool}, name=tool.name, arguments={"code": code}
            )
            invocation.start()
            assert invocation.result()["ok"]
    finally:
        manager.close()
    records = read_records(tmp_path)
    assert len(records) == 1
    assert records[0]["session_id"] == records[0]["root_session_id"] == "root"
    assert records[0]["parent_run_id"] == "parent"
    assert records[0]["sdk_run_id"] != "parent"


def test_completion_inherits_context_and_can_detach(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("YOKE_USAGE_METRIC_LOG_DIR", str(tmp_path))
    with usage_metric_context(
        surface="sdk", root_session_id="root", sdk_run_id="parent"
    ):
        complete("hello", provider=UsageProvider())
        complete("hello", provider=UsageProvider(), inherit_usage_attribution=False)
    attached, detached = read_records(tmp_path)
    assert attached["root_session_id"] == "root"
    assert attached["parent_run_id"] == "parent"
    assert "root_session_id" not in detached
    assert "parent_run_id" not in detached


def test_attribution_does_not_change_provider_requests(
    tmp_path: Path, monkeypatch
) -> None:
    from yoke.agent.models import Message
    from yoke.ai.sdk.types import Skill

    monkeypatch.setenv("YOKE_USAGE_METRIC_LOG_DIR", str(tmp_path))
    requests: list[object] = []

    class RecordingProvider(UsageProvider):
        def complete(
            self, messages: list[Message], tools: list[dict[str, object]]
        ) -> Message:
            requests.append(([m.model_dump() for m in messages], tools))
            return super().complete(messages, tools)

    for inherit in [False, True]:
        with usage_metric_context(surface="cli", session_id="owner"):
            agent = Agent(
                provider=RecordingProvider(),
                config=RunConfig(
                    root=tmp_path,
                    include_agents_file=False,
                    tools=["file.read", "shell"],
                    skills=[Skill.inline("example", "Read carefully.")],
                    inherit_usage_attribution=inherit,
                ),
            )
            try:
                agent.prompt("hello")
            finally:
                agent.close()
    assert requests[0] == requests[1]
    records = read_records(tmp_path)
    assert "session_id" not in records[0]
    assert records[1]["session_id"] == "owner"
