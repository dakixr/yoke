"""Run file-content fixtures locally when interpreter isolation is not the subject."""

from pathlib import Path

import pytest


LOCAL_TOOL_FILES = {
    "agent/test_tool_result_projection.py",
    "loop/test_agent_runtime_core.py",
    "cli/test_interactive_daemon.py",
    "cli/test_headless_daemon_parity.py",
    "http/test_attachment_continuation.py",
    "loop/test_interactive_daemon_projection.py",
    "provider_projection/test_attached_cli.py",
    "provider_projection/test_terminal_client.py",
}


@pytest.fixture(autouse=True)
def local_file_tools(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = request.node.path.relative_to(Path(__file__).parent).as_posix()
    parallel_oracle = (
        path == "provider_projection/test_oracle.py"
        and getattr(getattr(request.node, "callspec", None), "params", {}).get("name")
        == "parallel_tools"
    )
    if path in LOCAL_TOOL_FILES or parallel_oracle:
        from yoke.agent.tools import (
            AttachImageTool,
            ExecCommandTool,
            FdTool,
            ReadTool,
            RipgrepTool,
            WriteTool,
        )

        for tool in (
            ReadTool,
            WriteTool,
            RipgrepTool,
            ExecCommandTool,
            AttachImageTool,
            FdTool,
        ):
            monkeypatch.setattr(tool, "execute_in_process", True)
