"""Launch authority through tool workers and nested managed processes."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import pytest

from yoke.agent.loop.in_process_tool import InProcessToolInvocation
from yoke.agent.loop.tool_process import ToolProcessInvocation
from yoke.agent.tools import LocalTool
from yoke.agent.tools.command_process_manager import CommandProcessManager
from yoke.agent_runs.protocol import ENVIRONMENT_KEY
from yoke.ai.providers.usage_context import usage_metric_context


CHILD_CODE = """
from pathlib import Path
from yoke.ai import Agent, RunConfig
from yoke.agent.models import Message
class Provider:
    provider_name = 'fixture'
    supports_image_inputs = False
    max_images_per_message = None
    def complete(self, messages, tools):
        return Message.assistant('SECRET_OUTPUT')
agent = Agent(provider=Provider(), config=RunConfig(root=Path.cwd(), name='child', include_agents_file=False, root_session_id='forged-child-owner'))
try:
    agent.prompt('SECRET_PROMPT')
finally:
    agent.close()
"""


class SdkTool(LocalTool):
    name = "sdk_fixture"
    description = "Call an SDK agent without launching a command."

    def execute(self) -> dict[str, object]:
        exec(CHILD_CODE, {})
        return {"ok": True}


@pytest.mark.parametrize("isolated", [False, True])
def test_in_process_and_isolated_tools_bind_the_real_host(tmp_path, isolated):
    manager = CommandProcessManager()
    tool = SdkTool.bind(root=tmp_path, command_process_manager=manager)
    invocation = None
    try:
        with usage_metric_context(surface="cli", session_id="native-owner"):
            cls = ToolProcessInvocation if isolated else InProcessToolInvocation
            invocation = cls(tools={tool.name: tool}, name=tool.name, arguments={})
            invocation.start()
            assert invocation.result()["ok"]
        rows = manager.agent_runs.snapshots()
        assert len(rows) == 1
        assert rows[0]["sessionID"] == "native-owner"
        assert rows[0]["status"] == "completed"
        assert "SECRET" not in json.dumps(rows)
    finally:
        if isinstance(invocation, ToolProcessInvocation):
            invocation.close()
        manager.close()


def test_child_environment_override_cannot_replace_host_capability(tmp_path):
    manager = CommandProcessManager()
    env = {**os.environ, ENVIRONMENT_KEY: '{"address":"wrong","token":"forged"}'}
    try:
        with usage_metric_context(surface="cli", session_id="host-owner"):
            process = manager._spawn(
                "fixture",
                tmp_path,
                False,
                None,
                False,
                argv=[sys.executable, "-c", CHILD_CODE],
                env=env,
            )
        assert process.process.wait(timeout=10) == 0
        row = manager.agent_runs.snapshots()[0]
        assert row["sessionID"] == "host-owner"
        assert row["runtimeSessionID"] == process.session_id
    finally:
        manager.close()


def test_nested_manager_reports_only_to_original_registry(tmp_path: Path):
    # A child manager even with an explicit clean environment must forward host authority.
    code = f"""
import os, sys
from pathlib import Path
from yoke.agent.tools.command_process_manager import CommandProcessManager
manager = CommandProcessManager(base_environment={{'PATH': os.environ['PATH']}})
try:
    process = manager._spawn('grandchild', Path.cwd(), False, None, False,
        argv=[sys.executable, '-c', {CHILD_CODE!r}])
    assert process.process.wait(timeout=10) == 0
    assert manager.agent_runs.snapshots() == []
finally:
    manager.close()
"""
    manager = CommandProcessManager()
    try:
        with usage_metric_context(surface="http", session_id="original"):
            process = manager._spawn(
                "child", tmp_path, False, None, False, argv=[sys.executable, "-c", code]
            )
        assert process.process.wait(timeout=15) == 0
        rows = manager.agent_runs.snapshots()
        assert len(rows) == 1
        assert rows[0]["sessionID"] == "original"
        assert rows[0]["runtimeSessionID"] == process.session_id
    finally:
        manager.close()


def test_launcher_exit_does_not_revoke_a_live_descendant(tmp_path: Path):
    child = "import sys; sys.stdin.readline();\n" + CHILD_CODE
    launcher = (
        f"import subprocess,sys; subprocess.Popen([sys.executable, '-c', {child!r}])"
    )
    manager = CommandProcessManager()
    try:
        with usage_metric_context(surface="cli", session_id="host-owner"):
            managed = manager._spawn(
                "launcher",
                tmp_path,
                False,
                None,
                False,
                argv=[sys.executable, "-c", launcher],
            )
        assert managed.process.wait(timeout=5) == 0
        manager.agent_runs.reconcile()
        assert managed.process.stdin is not None
        managed.process.stdin.write(b"continue\n")
        managed.process.stdin.flush()
        with managed.condition:
            assert managed.condition.wait_for(lambda: managed.finished, timeout=10)
        rows = manager.agent_runs.snapshots()
        assert len(rows) == 1
        assert rows[0]["status"] == "completed"
        assert rows[0]["runtimeSessionID"] == managed.session_id
    finally:
        manager.close()
