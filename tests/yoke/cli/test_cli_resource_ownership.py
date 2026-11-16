from __future__ import annotations

# ruff: noqa: D100, D101, D102, D103, S101

from pathlib import Path
from typing import ClassVar

import pytest

from yoke.agent.loop.agent import RuntimeAgent
from yoke.agent.models import Message
from yoke.agent.tools import LocalTool
from yoke.cli.config import runtime as config_runtime


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


def test_daemon_owned_runtime_closes_tools_then_current_provider(tmp_path, monkeypatch):
    from .terminal_daemon_support import terminal_daemon
    from yoke.cli.backend.headless import run_headless_prompt
    from io import StringIO
    from tests.yoke.daemon.support import eventually

    closed = []

    class Provider(TrackingProvider):
        def close(self):
            closed.append("provider")
            super().close()

    class Resource(TrackingResource):
        def close(self):
            closed.append("tool")
            super().close()

    first_provider, current_provider = TrackingProvider(), Provider()
    resource = Resource()
    runtime = runtime_with_resource(tmp_path, first_provider, resource)
    first_provider.close()
    runtime.provider = current_provider
    with terminal_daemon(monkeypatch, lambda _record: runtime) as terminal:
        backend = terminal.backend("owned-resource", root=tmp_path)
        backend.title("Resource fixture", [])
        assert (
            run_headless_prompt(
                backend.connection,
                "own these resources",
                stdout=StringIO(),
                stderr=StringIO(),
            )
            == 0
        )
        backend.connection.close()
        assert current_provider.close_calls == 0
    eventually(lambda: current_provider.close_calls == 1)
    assert runtime.closed
    assert resource.close_calls == 1
    assert first_provider.close_calls == 1
    assert closed == ["tool", "provider"]


def test_daemon_closes_current_provider_when_tool_close_fails(tmp_path, monkeypatch):
    from .terminal_daemon_support import terminal_daemon
    from yoke.cli.backend.headless import run_headless_prompt
    from io import StringIO
    from tests.yoke.daemon.support import eventually

    provider = TrackingProvider()
    resource = TrackingResource(close_error=RuntimeError("tool close failed"))
    runtime = runtime_with_resource(tmp_path, provider, resource)
    with terminal_daemon(monkeypatch, lambda _record: runtime) as terminal:
        backend = terminal.backend("failed-tool-close", root=tmp_path)
        backend.title("Resource fixture", [])
        assert (
            run_headless_prompt(
                backend.connection, "close safely", stdout=StringIO(), stderr=StringIO()
            )
            == 0
        )
    eventually(lambda: provider.close_calls == 1)
    assert resource.close_calls == 1


def test_daemon_startup_preserves_construction_error_when_provider_close_fails(
    tmp_path, monkeypatch
):
    from .terminal_daemon_support import terminal_daemon
    from .support import CaptureStream
    from yoke.cli.backend.startup import run_daemon_cli
    from yoke.cli.config import CLIArgs

    provider = TrackingProvider(close_error=RuntimeError("provider close failed"))
    monkeypatch.setattr(config_runtime, "prepare_provider_args", lambda _args: None)
    monkeypatch.setattr(config_runtime, "_load_cli_skill_registry", lambda _root: None)
    monkeypatch.setattr(
        config_runtime, "build_provider_from_args", lambda _args: provider
    )

    def fail_resolution(**_kwargs):
        raise ValueError("original configuration failure")

    monkeypatch.setattr(config_runtime, "_resolve_cli_agent_config", fail_resolution)
    with terminal_daemon(monkeypatch):
        monkeypatch.setattr(
            "yoke.http.services.session_startup.build_cli_agent_from_args",
            config_runtime.build_cli_agent_from_args,
        )
        stderr = CaptureStream()
        assert (
            run_daemon_cli(
                CLIArgs(root=str(tmp_path)), stdout=CaptureStream(), stderr=stderr
            )
            == 1
        )
        assert "original configuration failure" in stderr.getvalue()
        assert "provider close failed" not in stderr.getvalue()
    assert provider.close_calls == 1
