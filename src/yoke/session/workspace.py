"""Live workspace validation and explicit, history-preserving relocation.

A saved session owns its history, not the directory named in its metadata.
Filesystem checks belong at execution boundaries, never in SessionStore.load.
Execution leases in daemon memory exclude relocation during owned work.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import errno
import os
from pathlib import Path
import stat
from typing import TYPE_CHECKING, Literal
from threading import Lock

from yoke.session.repository.models import SessionRecord
from yoke.session.repository.paths import validate_session_id
from yoke.session.repository.utils import timestamp

if TYPE_CHECKING:
    from yoke.agent.loop.agent import RuntimeAgent
    from yoke.session.repository.store import SessionStore


WorkspaceState = Literal[
    "available", "missing", "not_directory", "unreadable", "unconfigured", "invalid"
]


@dataclass(frozen=True, slots=True)
class WorkspaceStatus:
    """A fresh filesystem observation, never persisted as session identity."""

    status: WorkspaceState
    message: str | None = None

    @property
    def available(self) -> bool:
        return self.status == "available"


class WorkspaceError(ValueError):
    """Recoverable workspace error shared by terminal and HTTP callers."""

    code = "workspace_unavailable"

    def __init__(self, message: str, *, details: dict[str, object]) -> None:
        super().__init__(message)
        self.details = details


class WorkspaceUnavailable(WorkspaceError):
    """The saved directory cannot currently support execution."""

    def __init__(
        self,
        directory: Path | str | None,
        *,
        session_id: str | None = None,
        status: WorkspaceStatus | None = None,
    ) -> None:
        observed = status or inspect_workspace(directory)
        self.code = (
            "session_workspace_unavailable" if session_id else "workspace_unavailable"
        )
        message = observed.message or "Workspace is unavailable."
        if session_id:
            message += (
                " Saved history is intact. Restore the directory or explicitly "
                f"relocate with `yoke resume {session_id} --relocate /new/directory`."
            )
        super().__init__(
            message,
            details={
                "sessionID": session_id,
                "directory": str(directory) if directory is not None else None,
                "status": observed.status,
            },
        )


class WorkspaceBusy(WorkspaceError):
    """Relocation would change the directory beneath another owner or input."""

    code = "session_workspace_busy"

    def __init__(self, session_id: str, reason: str | None = None) -> None:
        super().__init__(
            reason
            or (
                "Session workspace is in use. Close other CLI sessions and stop "
                "active turns or processes before relocating."
            ),
            details={"sessionID": session_id},
        )


class WorkspaceConflict(WorkspaceError):
    """The caller's expected binding no longer matches durable metadata."""

    code = "session_workspace_conflict"

    def __init__(self, session_id: str, directory: str | None) -> None:
        super().__init__(
            "Session workspace changed. Reload the session before retrying.",
            details={"sessionID": session_id, "directory": directory},
        )


def inspect_workspace(directory: Path | str | None) -> WorkspaceStatus:
    """Observe availability without creating, repairing, or substituting paths."""
    if directory is None or not str(directory).strip():
        return WorkspaceStatus("unconfigured", "Session has no workspace directory.")
    label = str(directory)
    path = Path(directory).expanduser()
    try:
        mode = path.stat().st_mode
        if not stat.S_ISDIR(mode):
            return WorkspaceStatus(
                "not_directory", f"Workspace is not a directory: {label}"
            )
        if not os.access(path, os.R_OK | os.X_OK):
            return WorkspaceStatus(
                "unreadable", f"Workspace cannot be accessed: {label}"
            )
        # Resolve here as well so invalid parents and symlink loops get the same
        # domain error on every entry point.
        path.resolve()
    except FileNotFoundError:
        # Windows reports a missing leaf when an existing parent is a file,
        # while POSIX reports NotADirectoryError. Preserve one domain result.
        parent = path.parent
        while parent != parent.parent:
            try:
                mode = parent.stat().st_mode
            except FileNotFoundError:
                parent = parent.parent
                continue
            except OSError:
                break
            if not stat.S_ISDIR(mode):
                return WorkspaceStatus(
                    "not_directory", f"Workspace is not a directory: {label}"
                )
            break
        return WorkspaceStatus(
            "missing", f"Workspace directory no longer exists: {label}"
        )
    except NotADirectoryError:
        return WorkspaceStatus(
            "not_directory", f"Workspace is not a directory: {label}"
        )
    except (ValueError, RuntimeError):
        return WorkspaceStatus("invalid", f"Workspace path cannot be resolved: {label}")
    except OSError as exc:
        kind: WorkspaceState = "invalid" if exc.errno == errno.ELOOP else "unreadable"
        return WorkspaceStatus(kind, f"Workspace cannot be accessed: {label}")
    return WorkspaceStatus("available")


def require_workspace(
    directory: Path | str | None, *, session_id: str | None = None
) -> Path:
    """Resolve an executable directory or raise an actionable domain error."""
    observed = inspect_workspace(directory)
    if not observed.available:
        raise WorkspaceUnavailable(directory, session_id=session_id, status=observed)
    assert directory is not None
    try:
        return Path(directory).expanduser().resolve()
    except (OSError, ValueError, RuntimeError) as exc:
        raise WorkspaceUnavailable(
            directory,
            session_id=session_id,
            status=WorkspaceStatus(
                "invalid", "Workspace path changed while resolving it."
            ),
        ) from exc


def require_session_workspace(record: SessionRecord) -> Path:
    """Require the recorded directory, including for records without a root."""
    return require_workspace(record.root, session_id=record.id)


def promote_session_turn(primary: RuntimeAgent, forked: RuntimeAgent) -> None:
    """Accept completed conversation state even when its workspace disappears.

    Promotion transfers provider and conversation ownership before refreshing
    filesystem-dependent tools. That final refresh must not discard an answer
    that is already complete. The next execution still requires a valid root.
    """
    from yoke.agent.loop.forking import promote_runtime_fork

    try:
        promote_runtime_fork(primary, forked)
    except (OSError, ValueError):
        if inspect_workspace(primary.tool_root).available:
            raise


class WorkspaceLeases:
    """Nonblocking shared/exclusive ownership for daemon workspace bindings.

    Threads may release leases acquired by another thread after worker cleanup.
    Keys include the database so independent embedded applications stay isolated.
    """

    def __init__(self) -> None:
        self._lock = Lock()
        self._owners: dict[tuple[Path, str], tuple[int, bool]] = {}

    @contextmanager
    def acquire(
        self, database: Path, session_id: str, *, exclusive: bool
    ) -> Iterator[None]:
        key = (database.resolve(), session_id)
        with self._lock:
            readers, writer = self._owners.get(key, (0, False))
            if writer or (exclusive and readers):
                raise WorkspaceBusy(session_id)
            self._owners[key] = (readers + (not exclusive), exclusive)
        try:
            yield
        finally:
            with self._lock:
                readers, writer = self._owners[key]
                if exclusive or readers == 1:
                    del self._owners[key]
                else:
                    self._owners[key] = (readers - 1, writer)


_workspace_leases = WorkspaceLeases()


@contextmanager
def workspace_lease(
    store: SessionStore, session_id: str, *, exclusive: bool = False
) -> Iterator[None]:
    """Retain a daemon binding through persistence and physical resource cleanup."""
    validate_session_id(session_id)
    with _workspace_leases.acquire(
        store.database_path, session_id, exclusive=exclusive
    ):
        yield


def relocate_session_workspace(
    store: SessionStore,
    session_id: str,
    directory: Path | str,
    *,
    expected_root: str | None = None,
) -> SessionRecord:
    """Rebind a quiet session without rewriting any conversation entries.

    One queue transaction serializes admission with the durable root update. A
    failed validation or busy check writes neither history nor metadata.
    """
    from yoke.session.admissions import AdmissionStore
    from yoke.session.queue import prompt_queue_transaction
    from yoke.session.workspace_inputs import reconcile_workspace_inputs

    target = str(require_workspace(directory))
    with workspace_lease(store, session_id, exclusive=True):
        with prompt_queue_transaction(store.directory, session_id) as transaction:
            record = store.summary_record(session_id)
            if record is None or record.created_at is None:
                raise ValueError(f"Session not found: {session_id}")
            # Retrying a completed relocation is a no-op, even with the original
            # expected root. This does not permit changing to a third directory.
            if record.root != target:
                if expected_root is not None and record.root != expected_root:
                    raise WorkspaceConflict(session_id, record.root)
                queue = transaction.snapshot
                admission_store = AdmissionStore(store.directory)
                reconcile_workspace_inputs(
                    admission_store, session_id, transaction, orphaned=True
                )
                admissions = admission_store.load(session_id)
                if (
                    queue.prompts
                    or queue.pending_images
                    or any(
                        item.state == "promoted" and not item.settled
                        for item in admissions.records.values()
                    )
                ):
                    raise WorkspaceBusy(
                        session_id,
                        "Session has pending work. Remove or finish queued inputs before relocating.",
                    )
                # Recheck after obtaining both locks, not only before waiting on I/O.
                require_workspace(target)
                changes: dict[str, object] = {
                    "root": target,
                    "updated_at": timestamp(),
                    "context_usage": None,
                    "skill_dirs": [],
                }
                store.update_metadata(record.model_copy(update=changes), changes)
        # Preserve the full-record return contract without decoding history
        # while holding the database's cross-process writer lock.
        return store.load(session_id)
