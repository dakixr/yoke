"""DELETE WITH cli/runtime/{lifetime,workspaces,cli}.py.

These tests exercise invocation decorators, caller-injected ownership and local
lease ContextVars. They stay intact until those mechanisms are deleted. Daemon
coverage of the user-visible promises:

* Error-preserving cleanup and current-provider ownership:
  test_cli_resource_ownership::test_daemon_startup_preserves_construction_error_when_provider_close_fails
  test_cli_resource_ownership::test_daemon_closes_current_provider_when_tool_close_fails
  http/test_runtime_lifetime_regressions::test_agent_close_failure_still_closes_title_provider_and_is_logged
* Caller detachment leaves shared execution alive:
  http/test_daemon_api_environment::test_two_clients_share_prompts_detach_and_keep_admission_env_snapshot
* Lease exclusion through physical cleanup and background work:
  http/test_workspace_recovery::test_retired_worker_keeps_relocation_excluded_until_physical_completion
  http/test_workspace_lifecycle_regressions::test_background_command_keeps_a_lease_after_its_turn_is_idle
* Relocation binding and reload:
  test_cli_workspaces::test_relocation_preserves_history_and_noop_does_not_write
  http/test_workspace_recovery::test_relocation_preserves_identity_history_and_rejects_stale_or_invalid_targets

Exact covering names are also recorded in the implementation report. No daemon
client can receive a caller-owned RuntimeAgent, so borrowed/local object identity
assertions are specific to the modules being removed.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from threading import Event
import sys
import time
from yoke.agent.loop import RuntimeAgent
from yoke.agent.loop import in_process_tool as in_process_tools
from yoke.agent.tools import LocalTool
from yoke.cli.runtime.workspaces import retain_workspace_lease
from .test_cli_resource_ownership import (
    TrackingProvider,
    TrackingResource,
    runtime_with_resource,
)

import pytest

from yoke.agent.models import Message
from yoke.cli.config import CLIArgs
from yoke.cli.runtime import workspaces
from yoke.cli.runtime.cli import run_cli, run_resume_cli
from yoke.cli.runtime.lifetime import close_cli_owned_agent, register_cli_owned_agent
from yoke.cli.runtime.session import create_active_session, fork_active_session
from yoke.session import SessionStore
from yoke.session.workspace import (
    WorkspaceBusy,
    relocate_session_workspace,
    workspace_lease,
)

from .support import FakeAgent


def test_close_cli_owned_agent_preserves_entrypoint_error() -> None:
    class FailingCloseAgent:
        closed = False

        def close(self) -> None:
            self.closed = True
            raise RuntimeError("close failed")

    agent = FailingCloseAgent()

    @close_cli_owned_agent
    def entrypoint() -> None:
        register_cli_owned_agent(agent)
        raise ValueError("original failure")

    with pytest.raises(ValueError, match="original failure"):
        entrypoint()
    assert agent.closed is True


def test_close_cli_owned_agent_tolerates_close_failure() -> None:
    class FailingCloseAgent:
        closed = False

        def close(self) -> None:
            self.closed = True
            raise RuntimeError("close failed")

    agent = FailingCloseAgent()

    @close_cli_owned_agent
    def entrypoint() -> str:
        register_cli_owned_agent(agent)
        return "ok"

    assert entrypoint() == "ok"
    assert agent.closed is True


def test_close_cli_owned_agent_leaves_caller_owned_agent() -> None:
    class CloseTrackingAgent:
        closed = False

        def close(self) -> None:
            self.closed = True

    agent = CloseTrackingAgent()

    @close_cli_owned_agent
    def entrypoint(agent: object | None = None) -> str:
        return "ok"

    assert entrypoint(agent=agent) == "ok"
    assert agent.closed is False


@pytest.mark.parametrize("mode", ["resume", "session"])
def test_use_lease_precedes_load_and_survives_switches_and_cleanup(
    tmp_path: Path, monkeypatch, mode: str
) -> None:
    store = SessionStore()
    store.save("saved", [Message.user("old")], root=tmp_path)
    real_load = SessionStore.load
    seen_ids = ["saved"]

    def load(self, session_id):
        with pytest.raises(WorkspaceBusy):
            with workspace_lease(self, session_id, exclusive=True):
                pass
        return real_load(self, session_id)

    monkeypatch.setattr(SessionStore, "load", load)

    def interactive(_args, agent, messages, **kwargs):
        active = kwargs["active_session"]
        fork = fork_active_session(active, agent, messages)
        new = create_active_session(CLIArgs(root=str(tmp_path)), root=tmp_path)
        seen_ids.extend([fork.id, new.id])
        for session_id in seen_ids:
            with pytest.raises(WorkspaceBusy):
                with workspace_lease(store, session_id, exclusive=True):
                    pass
        return 0

    monkeypatch.setattr("yoke.cli.interactive.run_interactive_cli", interactive)
    if mode == "resume":
        assert (
            run_resume_cli(CLIArgs(root=str(tmp_path)), "saved", agent=FakeAgent()) == 0
        )
    else:
        assert (
            run_cli(CLIArgs(root=str(tmp_path), session="saved"), agent=FakeAgent())
            == 0
        )
    for session_id in seen_ids:
        with workspace_lease(store, session_id, exclusive=True):
            pass


def test_reload_after_relocation_observes_binding_under_use_lease(
    tmp_path: Path, monkeypatch
) -> None:
    store = SessionStore()
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    store.save("saved", [], root=tmp_path)
    real_lease = workspaces.workspace_lease

    @contextmanager
    def raced_lease(store, session_id):
        relocate_session_workspace(store, session_id, second)
        with real_lease(store, session_id):
            yield

    monkeypatch.setattr(workspaces, "workspace_lease", raced_lease)

    def interactive(args, _agent, _messages, **kwargs):
        assert args.root == str(second)
        assert kwargs["active_session"].root == second
        return 0

    monkeypatch.setattr("yoke.cli.interactive.run_interactive_cli", interactive)
    assert (
        run_resume_cli(
            CLIArgs(root=str(tmp_path)), "saved", relocate=first, agent=FakeAgent()
        )
        == 0
    )


# DELETE WITH cli/runtime/lifetime.py and workspaces.py. These invocation-owner
# mechanisms have no daemon client counterpart. User-visible coverage lives in:
# http/test_runtime_lifetime_regressions.py::test_primary_runtime_provider_closes_once_at_lifespan_exit
# http/test_runtime_lifetime_regressions.py::test_agent_close_failure_still_closes_title_provider_and_is_logged
# http/test_runtime_resource_regressions.py::test_nonterminal_in_process_fork_close_retains_every_owner_until_retry
# http/test_workspace_lifecycle_regressions.py::test_background_command_keeps_a_lease_after_its_turn_is_idle
# http/test_workspace_recovery.py::test_retired_worker_keeps_relocation_excluded_until_physical_completion
def test_cli_owned_runtime_closes_tools_then_current_provider(tmp_path: Path) -> None:
    first_provider = TrackingProvider()
    current_provider = TrackingProvider()
    resource = TrackingResource()
    runtime = runtime_with_resource(tmp_path, first_provider, resource)
    first_provider.close()
    runtime.provider = current_provider

    @close_cli_owned_agent
    def entrypoint() -> str:
        register_cli_owned_agent(runtime)
        return "ok"

    assert entrypoint() == "ok"
    assert runtime._closed is True
    assert resource.close_calls == 1
    assert first_provider.close_calls == 1
    assert current_provider.close_calls == 1


def test_cli_closes_current_provider_when_runtime_close_fails(tmp_path: Path) -> None:
    provider = TrackingProvider()
    resource = TrackingResource(close_error=RuntimeError("tool close failed"))
    runtime = runtime_with_resource(tmp_path, provider, resource)

    @close_cli_owned_agent
    def entrypoint() -> str:
        register_cli_owned_agent(runtime)
        return "ok"

    assert entrypoint() == "ok"
    assert resource.close_calls == 1
    assert provider.close_calls == 1


def test_cli_retains_provider_until_detached_tool_finishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started, release, finished, provider_closed = Event(), Event(), Event(), Event()
    store = SessionStore()
    store.save("detached", [], root=tmp_path)

    class Provider(TrackingProvider):
        def close(self) -> None:
            assert finished.is_set(), "Provider closed while its tool was active"
            with pytest.raises(WorkspaceBusy):
                with workspace_lease(store, "detached", exclusive=True):
                    pass
            super().close()
            provider_closed.set()

    class BlockingTool(LocalTool):
        name = "block_until_released"
        description = "Wait for the test to release this in-process worker."
        execute_in_process = True

        def execute(self) -> dict[str, object]:
            started.set()
            release.wait(timeout=3)
            finished.set()
            return {"ok": True}

    provider = Provider()
    runtime = RuntimeAgent(provider, [BlockingTool.bind(root=tmp_path)])
    monkeypatch.setattr(in_process_tools, "IN_PROCESS_TOOL_SHUTDOWN_SECONDS", 0)

    @close_cli_owned_agent
    def entrypoint() -> None:
        retain_workspace_lease(store, "detached")
        register_cli_owned_agent(runtime)
        in_process_tools.execute_in_process_tool(
            tools=runtime.tools,
            name=BlockingTool.name,
            arguments={},
            stop_requested=lambda: started.is_set(),
        )

    try:
        entrypoint()
        assert started.is_set()
        assert provider.close_calls == 0
        assert not runtime._closed
        with pytest.raises(WorkspaceBusy):
            with workspace_lease(store, "detached", exclusive=True):
                pass
        release.set()
        assert provider_closed.wait(timeout=2)
        assert runtime._closed is True
        assert provider.close_calls == 1
    finally:
        release.set()
        finished.wait(timeout=2)
        monkeypatch.setattr(in_process_tools, "IN_PROCESS_TOOL_SHUTDOWN_SECONDS", 1)
        runtime.close()


def test_cli_does_not_close_caller_supplied_runtime(tmp_path: Path) -> None:
    provider = TrackingProvider()
    resource = TrackingResource()
    runtime = runtime_with_resource(tmp_path, provider, resource)

    @close_cli_owned_agent
    def entrypoint(agent: RuntimeAgent | None = None) -> str:
        assert agent is runtime
        return "ok"

    assert entrypoint(agent=runtime) == "ok"
    assert runtime._closed is False
    assert resource.close_calls == 0
    assert provider.close_calls == 0

    runtime.close()


def test_borrowed_runtime_keeps_workspace_lease_until_background_process_finishes(
    tmp_path: Path,
) -> None:
    provider = TrackingProvider()
    resource = TrackingResource()
    runtime = runtime_with_resource(tmp_path, provider, resource)
    store = SessionStore()
    target = tmp_path / "target"
    target.mkdir()
    store.save("borrowed", [], root=tmp_path)

    @close_cli_owned_agent
    def entrypoint(agent: RuntimeAgent | None = None) -> int:
        assert agent is runtime
        retain_workspace_lease(store, "borrowed")
        result = runtime.command_process_manager.exec_argv(
            argv=[sys.executable, "-c", "import time; time.sleep(0.4)"],
            display_command="sleeping child",
            cwd=tmp_path,
            env=runtime.command_process_manager.base_environment(),
            yield_time_ms=1,
            timeout_seconds=None,
            cancel_requested=None,
        )
        assert result.session_id is not None
        return result.session_id

    session_id = entrypoint(agent=runtime)
    with pytest.raises(WorkspaceBusy):
        with workspace_lease(store, "borrowed", exclusive=True):
            pass
    deadline = time.monotonic() + 3
    while runtime.command_process_manager.snapshot(session_id).status == "running":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    while True:
        try:
            with workspace_lease(store, "borrowed", exclusive=True):
                break
        except WorkspaceBusy:
            assert time.monotonic() < deadline
            time.sleep(0.01)
    assert runtime._closed is False
    assert provider.close_calls == 0
    runtime.close()


def test_closing_borrowed_runtime_releases_retained_workspace_lease(
    tmp_path: Path,
) -> None:
    provider = TrackingProvider()
    resource = TrackingResource()
    runtime = runtime_with_resource(tmp_path, provider, resource)
    store = SessionStore()
    store.save("borrowed-close", [], root=tmp_path)

    @close_cli_owned_agent
    def entrypoint(agent: RuntimeAgent | None = None) -> None:
        assert agent is runtime
        retain_workspace_lease(store, "borrowed-close")
        result = runtime.command_process_manager.exec_argv(
            argv=[sys.executable, "-c", "import time; time.sleep(5)"],
            display_command="long child",
            cwd=tmp_path,
            env=runtime.command_process_manager.base_environment(),
            yield_time_ms=1,
            timeout_seconds=None,
            cancel_requested=None,
        )
        assert result.session_id is not None

    entrypoint(agent=runtime)
    with pytest.raises(WorkspaceBusy):
        with workspace_lease(store, "borrowed-close", exclusive=True):
            pass
    runtime.close()
    deadline = time.monotonic() + 3
    while True:
        try:
            with workspace_lease(store, "borrowed-close", exclusive=True):
                break
        except WorkspaceBusy:
            assert time.monotonic() < deadline
            time.sleep(0.01)


def test_borrowed_runtime_retains_workspace_for_detached_in_process_tool(
    tmp_path: Path,
) -> None:
    started, release, finished = Event(), Event(), Event()
    provider = TrackingProvider()
    store = SessionStore()
    store.save("borrowed-tool", [], root=tmp_path)

    class BlockingTool(LocalTool):
        name = "borrowed_block"
        description = "Stay physically active after logical cancellation."
        execute_in_process = True

        def execute(self) -> dict[str, object]:
            started.set()
            release.wait(timeout=3)
            finished.set()
            return {"ok": True}

    runtime = RuntimeAgent(provider, [BlockingTool.bind(root=tmp_path)])

    @close_cli_owned_agent
    def entrypoint(agent: RuntimeAgent | None = None) -> None:
        assert agent is runtime
        retain_workspace_lease(store, "borrowed-tool")
        _result, cancelled = in_process_tools.execute_in_process_tool(
            tools=runtime.tools,
            name=BlockingTool.name,
            arguments={},
            stop_requested=lambda: started.is_set(),
        )
        assert cancelled

    try:
        entrypoint(agent=runtime)
        assert started.is_set() and not finished.is_set()
        with pytest.raises(WorkspaceBusy):
            with workspace_lease(store, "borrowed-tool", exclusive=True):
                pass
        release.set()
        assert finished.wait(timeout=2)
        deadline = time.monotonic() + 3
        while True:
            try:
                with workspace_lease(store, "borrowed-tool", exclusive=True):
                    break
            except WorkspaceBusy:
                assert time.monotonic() < deadline
                time.sleep(0.01)
        assert runtime.closed is False
        assert provider.close_calls == 0
    finally:
        release.set()
        runtime.close()
