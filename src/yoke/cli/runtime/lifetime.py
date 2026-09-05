"""Ownership tracking for runtimes constructed by CLI entrypoints."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from contextvars import ContextVar
from functools import wraps
from inspect import signature
import logging
from threading import Thread
from typing import cast

from yoke.agent.loop.agent import RuntimeAgent
from yoke.agent.loop.in_process_tool import InProcessToolShutdownError
from yoke.agent.loop.in_process_tool import wait_for_in_process_tools

LOGGER = logging.getLogger(__name__)
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
        if arguments.get("agent") is not None:
            return function(*args, **kwargs)
        token = _OWNED_AGENT.set(None)
        try:
            return function(*args, **kwargs)
        finally:
            owned_agent = _OWNED_AGENT.get()
            _OWNED_AGENT.reset(token)
            with suppress(Exception):
                _close_cli_owned_runtime(owned_agent)

    return wrapped


def register_cli_owned_agent(agent: object) -> None:
    """Register the runtime created within the current CLI invocation."""
    _OWNED_AGENT.set(agent)


def _close_cli_owned_runtime(agent: object | None) -> None:
    """Close a CLI-owned runtime followed by its current provider."""
    try:
        close = getattr(agent, "close", None)
        if callable(close):
            cast(Callable[[], None], close)()
    except InProcessToolShutdownError:
        if isinstance(agent, RuntimeAgent) and not agent.closed:
            Thread(
                target=_finish_detached_runtime,
                args=(agent,),
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


def _finish_detached_runtime(agent: RuntimeAgent) -> None:
    """Retain ownership until cancelled in-process tools release the provider."""
    try:
        wait_for_in_process_tools(agent.tools)
        _close_cli_owned_runtime(agent)
    except Exception:
        LOGGER.exception("Failed to close a retired CLI runtime.")
