"""Actionable storage diagnostics shared by daemon startup and CLI commands."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from yoke.storage.schema import SchemaTooNewError


def database_open_error(path: Path, cause: BaseException) -> str:
    """Name the database, cause and recovery action without a traceback."""
    prefix = f"Yoke database startup failed: {path}: {cause}."
    if isinstance(cause, SchemaTooNewError):
        return prefix + " Upgrade Yoke to match the database schema."
    if isinstance(cause, sqlite3.Error) and getattr(
        cause, "sqlite_errorcode", None
    ) in {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    }:
        return prefix + " Close other database writers, then retry. Run yoke db check."
    return (
        prefix + " Run yoke db check. If damaged, stop Yoke clients and restore "
        "the latest adjacent yoke.db.pre-v<N> backup before starting again."
    )
