"""Cleanup for optional resources created while configuring or running the CLI."""

from typing import Protocol, cast


class Closeable(Protocol):
    """Resource with explicit synchronous cleanup."""

    def close(self) -> None: ...


def close_quietly(resource: object, *, suppress_errors: bool = True) -> None:
    """Close a supported resource, optionally preserving shutdown failures."""
    # Dynamic adapters, including proxy objects, may expose close via __getattr__.
    if not callable(getattr(resource, "close", None)):
        return
    try:
        cast(Closeable, resource).close()
    except Exception:
        if not suppress_errors:
            raise
