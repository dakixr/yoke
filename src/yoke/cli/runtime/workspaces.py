"""Workspace bindings retained for the lifetime of a CLI invocation."""

from __future__ import annotations

from contextlib import ExitStack
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING

from yoke.cli.session import SessionRecord, SessionStore
from yoke.session.workspace import relocate_session_workspace
from yoke.session.workspace import require_session_workspace
from yoke.session.workspace import workspace_lease

if TYPE_CHECKING:
    from yoke.agent.loop.agent import RuntimeAgent

_turn_leases: dict[RuntimeAgent, ExitStack] = {}
_turn_leases_lock = Lock()


def retain_turn_workspace(agent: RuntimeAgent, leases: ExitStack) -> None:
    """Transfer a turn's lease to the runtime that owns its detached tools."""
    with _turn_leases_lock:
        _turn_leases[agent] = leases.pop_all()


def take_turn_workspace(agent: RuntimeAgent) -> ExitStack:
    """Transfer a retired turn's lease to its cleanup thread."""
    with _turn_leases_lock:
        return _turn_leases.pop(agent, ExitStack())


@dataclass
class CLIWorkspaces:
    """Keep every visited session locked until runtime cleanup finishes."""

    leases: ExitStack = field(default_factory=ExitStack)
    retained: set[tuple[Path, str]] = field(default_factory=set)


cli_workspaces: ContextVar[CLIWorkspaces | None] = ContextVar(
    "yoke_cli_workspaces", default=None
)


def retain_workspace_lease(store: SessionStore, session_id: str) -> None:
    """Acquire before reading metadata, once per session per invocation.

    Standalone persistence helpers do not own an invocation. Their callers
    remain responsible for lifetime management.
    """
    owner = cli_workspaces.get()
    if owner is None:
        return
    key = (store.directory.resolve(), session_id)
    if key not in owner.retained:
        owner.leases.enter_context(workspace_lease(store, session_id))
        owner.retained.add(key)


def session_workspace(record: SessionRecord) -> Path:
    """Require an explicit workspace binding for every saved record."""
    return require_session_workspace(record)


def load_resume_workspace(
    store: SessionStore,
    session_id: str,
    *,
    relocate: Path | str | None = None,
) -> SessionRecord:
    """Relocate explicitly, then reload under the invocation's use lease."""
    if relocate is not None:
        relocate_session_workspace(store, session_id, relocate)
    retain_workspace_lease(store, session_id)
    # Another owner may have relocated after the exclusive lease was released.
    # Never construct a runtime from the relocation operation's returned record.
    record = store.load(session_id)
    if not store.exists(session_id):
        raise ValueError(f"Session not found: {session_id}")
    return record
