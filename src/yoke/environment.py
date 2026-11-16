"""Explicit, memory-only execution environments for daemon-owned work.

Work outside a daemon client scope leaves this unset and keeps the process environment.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ExecutionEnvironment:
    values: Mapping[str, str] = field(repr=False)
    cwd: Path | None = field(default=None, repr=False)


_CURRENT: ContextVar[ExecutionEnvironment | None] = ContextVar(
    "yoke_execution_environment", default=None
)


def current_environment() -> ExecutionEnvironment | None:
    """Return the current daemon operation's explicit environment, if any."""
    return _CURRENT.get()


def environment_values() -> Mapping[str, str]:
    """Keep process inheritance only for callers without an explicit scope."""
    current = current_environment()
    return os.environ if current is None else current.values


@contextmanager
def execution_environment(value: ExecutionEnvironment | None) -> Iterator[None]:
    """Bind provider resolution during construction, forks and model switches."""
    token = _CURRENT.set(value)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def project_environment(
    root: Path, values: Mapping[str, str], *, cwd: Path | None = None
) -> ExecutionEnvironment:
    """Load project dotenv into a copy, with explicit client values winning."""
    from yoke.cli.main_core import dotenv_values

    merged = dotenv_values(root)
    merged.update(values)
    return ExecutionEnvironment(merged, cwd)


_REDACTOR: ContextVar[Callable[[object], object] | None] = ContextVar(
    "yoke_observation_redactor", default=None
)


@contextmanager
def observation_redaction(redact: Callable[[object], object]) -> Iterator[None]:
    """Bind output sanitation without changing provider-bound values."""
    token = _REDACTOR.set(redact)
    try:
        yield
    finally:
        _REDACTOR.reset(token)


def redact_observation(value: object) -> object:
    """Sanitize a daemon log copy; ordinary SDK/CLI logs keep their behavior."""
    redact = _REDACTOR.get()
    return value if redact is None else redact(value)
