"""Capture cleanup started during worker preparation, including failed forks."""

from collections.abc import Callable
from concurrent.futures import Future
from contextvars import ContextVar

from yoke.http.services.session_runtime.reaper import retire_resource

_owned: ContextVar[list[Future[None]] | None] = ContextVar("turn_cleanup", default=None)


def execute_with_cleanup[T](work: Callable[[], T], pending: list[Future[None]]) -> T:
    token = _owned.set(pending)
    try:
        return work()
    finally:
        _owned.reset(token)


def retire_owned_resource(attempt: Callable[[], bool]) -> Future[None]:
    completion = retire_resource(attempt)
    pending = _owned.get()
    if pending is not None:
        pending.append(completion)
    return completion
