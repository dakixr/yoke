"""Ownership tracking for runtimes constructed by CLI entrypoints."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import ExitStack, suppress
from contextvars import ContextVar
from functools import wraps
from inspect import signature
import logging
from threading import Thread
import time
from typing import cast

from yoke.agent.loop.agent import RuntimeAgent
from yoke.agent.loop.in_process_tool import InProcessToolShutdownError
from yoke.agent.loop.in_process_tool import wait_for_in_process_tools
from yoke.cli.runtime.workspaces import CLIWorkspaces, cli_workspaces

LOGGER = logging.getLogger(__name__)
BORROWED_WORKSPACE_POLL_SECONDS = 0.02
_OWNED_AGENT: ContextVar[object | None] = ContextVar(
    "yoke_cli_owned_agent", default=None
)


def close_cli_owned_agent[**P, R](
    function: Callable[P, R],
) -> Callable[P, R]:
    """Close a runtime built inside one CLI entrypoint invocation."""
    function_signature = signature(function)

    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        arguments = function_signature.bind_partial(*args, **kwargs).arguments
        borrowed = arguments.get("agent") is not None
        workspaces = CLIWorkspaces()
        workspace_token = cli_workspaces.set(workspaces)
        token = _OWNED_AGENT.set(None)
        try:
            return function(*args, **kwargs)
        finally:
            owned_agent = _OWNED_AGENT.get()
            _OWNED_AGENT.reset(token)
            try:
                if borrowed:
                    with suppress(Exception):
                        _retain_borrowed_runtime_workspaces(
                            arguments.get("agent"), workspaces.leases
                        )
                else:
                    with suppress(Exception):
                        _close_cli_owned_runtime(owned_agent, workspaces.leases)
            finally:
                cli_workspaces.reset(workspace_token)
                workspaces.leases.close()

    return wrapped


def register_cli_owned_agent(agent: object) -> None:
    """Register the runtime created within the current CLI invocation."""
    _OWNED_AGENT.set(agent)


def _retain_borrowed_runtime_workspaces(
    agent: object | None, leases: ExitStack
) -> None:
    """Keep borrowed-session leases until detached physical work is quiet."""
    if not isinstance(agent, RuntimeAgent):
        return
    # Capture the tool map before a caller can close the borrowed runtime and
    # replace ``agent.tools``. Detached in-process workers are keyed by this
    # exact mapping identity.
    tool_map = agent.tools
    manager = agent.command_process_manager
    retained = leases.pop_all()

    def release_when_quiet() -> None:
        try:
            # Logical cancellation can return before an in-process worker exits.
            # Do not release the binding until those workers physically finish.
            wait_for_in_process_tools(tool_map)
            while any(item.status == "running" for item in manager.snapshots()):
                # Polling is intentional. ``CommandProcessManager.close`` clears
                # listeners before terminating children, so callback ownership
                # cannot be the lifetime signal for a borrowed runtime.
                time.sleep(BORROWED_WORKSPACE_POLL_SECONDS)
        finally:
            retained.close()

    Thread(
        target=release_when_quiet,
        daemon=True,
        name="yoke-cli-borrowed-workspace",
    ).start()


def _close_cli_owned_runtime(
    agent: object | None, leases: ExitStack | None = None
) -> None:
    """Close a CLI-owned runtime followed by its current provider."""
    try:
        close = getattr(agent, "close", None)
        if callable(close):
            cast(Callable[[], None], close)()
    except InProcessToolShutdownError:
        if isinstance(agent, RuntimeAgent) and not agent.closed:
            Thread(
                target=_finish_detached_runtime,
                args=(agent, leases.pop_all() if leases is not None else ExitStack()),
                daemon=True,
                name="yoke-cli-runtime-reaper",
            ).start()
        raise
    finally:
        if not isinstance(agent, RuntimeAgent) or agent.closed:
            provider = getattr(agent, "provider", None)
            close_provider = getattr(provider, "close", None)
            if callable(close_provider):
                cast(Callable[[], None], close_provider)()


def _finish_detached_runtime(agent: RuntimeAgent, leases: ExitStack) -> None:
    """Retain ownership until cancelled in-process tools release the provider."""
    try:
        wait_for_in_process_tools(agent.tools)
        _close_cli_owned_runtime(agent)
    except Exception:
        LOGGER.exception("Failed to close a retired CLI runtime.")
    finally:
        leases.close()
