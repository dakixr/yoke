"""Session operations for daemon clients and injected in-process oracles."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from yoke.cli.backend.session import SessionBackend as SessionBackend
    from yoke.cli.backend.daemon import DaemonSessionBackend as DaemonSessionBackend


def __getattr__(name: str) -> object:
    if name == "SessionBackend":
        from yoke.cli.backend.session import SessionBackend

        return SessionBackend
    if name == "DaemonSessionBackend":
        from yoke.cli.backend.daemon import DaemonSessionBackend

        return DaemonSessionBackend
    raise AttributeError(name)
