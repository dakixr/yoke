"""Compatibility imports for runtime callers of the removed read cache."""

from yoke.http.reads.snapshots import SessionReadSnapshot, SessionSnapshots

SessionReadCache = SessionSnapshots

__all__ = ["SessionReadCache", "SessionReadSnapshot"]
