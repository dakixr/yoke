"""Reserve promoted input ownership before scheduling physical workers."""

from __future__ import annotations

from contextlib import ExitStack
from typing import TYPE_CHECKING

from yoke.http.services.runtime_persistence import (
    input_has_terminal_assistant,
    input_is_persisted,
)
from yoke.http.services.session_runtime.execution import ReservedInput
from yoke.http.services.session_runtime.workspace import workspace_ready
from yoke.session.workspace import workspace_lease

if TYPE_CHECKING:
    from yoke.http.services.runtime import SessionRuntime


def reserve_next_input(
    runtime: SessionRuntime, *, allow_queue: bool = True, recover: bool = True
) -> ReservedInput | None:
    """Cover promotion, waiting for capacity, execution, and cleanup with one lease.

    A promoted input without this lease is an orphan after a process exits.
    Acquiring before promotion makes that distinction safe across processes.
    """
    with ExitStack() as use:
        use.enter_context(workspace_lease(runtime.store, runtime.session_id))
        if not workspace_ready(runtime):
            return None
        if recover:
            while True:
                promoted = runtime.pending_inputs.unsettled_promoted(runtime.session_id)
                if promoted is None:
                    break
                snapshot = runtime._snapshot()
                if not input_is_persisted(snapshot.record, promoted.id):
                    return ReservedInput(promoted, use.pop_all())
                if not input_has_terminal_assistant(
                    snapshot.active_path_entries, promoted.id
                ):
                    runtime._persist_interrupted_checkpoint(promoted)
                runtime.pending_inputs.settle(
                    runtime.session_id, promoted.id, outcome="recovered"
                )
        admission = runtime.pending_inputs.pop_next(
            runtime.session_id, allow_queue=allow_queue
        )
        return (
            ReservedInput(admission, use.pop_all()) if admission is not None else None
        )
