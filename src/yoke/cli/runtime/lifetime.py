"""Ownership tracking for runtimes constructed by CLI entrypoints."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from contextvars import ContextVar
from functools import wraps
from inspect import signature
from typing import cast

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
            close = getattr(owned_agent, "close", None)
            if callable(close):
                with suppress(Exception):
                    cast(Callable[[], None], close)()

    return wrapped


def register_cli_owned_agent(agent: object) -> None:
    """Register the runtime created within the current CLI invocation."""
    _OWNED_AGENT.set(agent)
