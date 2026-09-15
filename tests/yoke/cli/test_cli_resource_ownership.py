from __future__ import annotations

# ruff: noqa: D100, D101, D102, D103, S101

from pathlib import Path
from threading import Event
import sys
import time
from typing import ClassVar

import pytest

from yoke.agent.loop.agent import RuntimeAgent
from yoke.agent.loop import in_process_tool as in_process_tools
from yoke.agent.models import Message
from yoke.agent.tools import LocalTool
from yoke.cli.config import runtime as config_runtime
from yoke.cli.runtime.lifetime import close_cli_owned_agent
from yoke.cli.runtime.lifetime import register_cli_owned_agent
from yoke.cli.runtime.workspaces import retain_workspace_lease
from yoke.cli.session import SessionStore
from yoke.session.workspace import WorkspaceBusy, workspace_lease


class TrackingProvider:
    supports_image_inputs: ClassVar[bool] = False
    max_images_per_message: ClassVar[int | None] = None

    def __init__(self, *, close_error: BaseException | None = None) -> None:
        self.close_calls = 0
        self.close_error = close_error

    def complete(
        self,
        messages: list[Message],
        tools: list[dict[str, object]],
    ) -> Message:
        del messages, tools
        return Message.assistant("done")

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class ResourceTool(LocalTool):
    name = "resource"
    description = "Own one close-tracking resource."

    def execute(self) -> dict[str, object]:
        return {"ok": True}

    def owned_resources(self) -> tuple[object, ...]:
        return (self._context["resource"],)


class TrackingResource:
    def __init__(self, *, close_error: BaseException | None = None) -> None:
        self.close_calls = 0
        self.close_error = close_error

    def close(self) -> None:
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


def runtime_with_resource(
    tmp_path: Path,
    provider: TrackingProvider,
    resource: TrackingResource,
) -> RuntimeAgent:
    return RuntimeAgent(
        provider=provider,
        tools=[ResourceTool.bind(resource=resource)],
        tool_root=tmp_path,
    )


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


def test_cli_construction_failure_closes_provider_and_keeps_original_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = TrackingProvider(close_error=RuntimeError("provider close failed"))
    original_error = ValueError("config failed")
    monkeypatch.setattr(config_runtime, "prepare_provider_args", lambda _args: None)
    monkeypatch.setattr(config_runtime, "_load_cli_skill_registry", lambda _root: None)
    monkeypatch.setattr(
        config_runtime,
        "build_provider_from_args",
        lambda _args: provider,
    )

    def fail_resolution(**_kwargs: object) -> None:
        raise original_error

    monkeypatch.setattr(config_runtime, "_resolve_cli_agent_config", fail_resolution)

    with pytest.raises(ValueError) as raised:
        config_runtime.build_cli_agent_from_args(
            config_runtime.CLIArgs(root=str(tmp_path))
        )

    assert raised.value is original_error
    assert provider.close_calls == 1
