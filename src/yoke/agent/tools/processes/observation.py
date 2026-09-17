"""Repeatable output pages and completion-first waits shared by native and MCP."""

from __future__ import annotations

from collections.abc import Callable
import threading
import time
from typing import Any

from yoke.agent.tools.command_process_manager import CommandProcessManager
from yoke.agent.tools.command_process_types import CommandProcessOutputPage
from yoke.agent.tools.command_process_types import CommandProcessSnapshot
from yoke.agent.tools.processes.cursor import decode_cursor
from yoke.agent.tools.processes.cursor import encode_cursor
from yoke.agent.tools.processes.cursor import ProcessPosition
from yoke.agent.tools.processes.models import ProcessReadRequest


def _position(
    cursor: ProcessPosition, history: CommandProcessOutputPage
) -> tuple[int, int, bool]:
    """Validate a cursor against retained history before waiting or sending input."""
    if cursor.after_seq > history.latest_seq:
        raise ValueError("Invalid process cursor")
    gap = cursor.after_seq < history.truncated_before_seq
    if gap:
        return history.truncated_before_seq, 0, True
    if cursor.offset:
        if not history.chunks:
            raise ValueError("Invalid process cursor")
        raw = history.chunks[0].text.encode("utf-8")
        if cursor.offset >= len(raw):
            raise ValueError("Invalid process cursor")
        try:
            raw[: cursor.offset].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Invalid process cursor") from exc
    return cursor.after_seq, cursor.offset, False


def page(
    manager: CommandProcessManager,
    cursor: ProcessPosition,
    budget: int,
) -> dict[str, Any]:
    """Read retained output without consuming it or advancing past unseen bytes."""
    snapshot, history = manager.observe_output(
        cursor.session_id, after_seq=cursor.after_seq, limit=100
    )
    seq, offset, gap = _position(cursor, history)
    latest_seq = history.latest_seq
    truncated_before_seq = history.truncated_before_seq
    fragments: list[str] = []
    remaining = max(0, budget)
    done = False
    while not done:
        for chunk in history.chunks:
            if chunk.seq > latest_seq:
                done = True
                break
            raw = chunk.text.encode("utf-8")
            fragment = raw[offset : offset + remaining].decode("utf-8", errors="ignore")
            fragments.append(fragment)
            used = len(fragment.encode("utf-8"))
            remaining -= used
            if offset + used < len(raw):
                offset += used
                done = True
                break
            seq, offset = chunk.seq, 0
        if done or seq >= latest_seq or remaining == 0 or not history.chunks:
            break
        history = manager.output_chunks(
            cursor.session_id, after_seq=seq, limit=100, exact=True
        )
        if history.truncated_before_seq > seq:
            gap = True
            truncated_before_seq = history.truncated_before_seq
            seq, offset = min(truncated_before_seq, latest_seq), 0
    running = snapshot.status == "running"
    has_more = seq < latest_seq or offset > 0
    result: dict[str, Any] = {
        "ok": True,
        "session_id": cursor.session_id,
        "status": snapshot.status,
        "running": running,
        "exit_code": snapshot.exit_code,
        "timed_out": snapshot.timed_out,
        "output": "".join(fragments),
        "elapsed_seconds": snapshot.elapsed_seconds,
        "cursor": encode_cursor(
            ProcessPosition(session_id=cursor.session_id, after_seq=seq, offset=offset)
        ),
        "has_more_output": has_more,
        "gap": gap,
    }
    if running or has_more:
        result["next_tool"] = "process_read"
    return result


def _state(
    manager: CommandProcessManager, cursor: ProcessPosition
) -> tuple[CommandProcessSnapshot, bool]:
    snapshot, history = manager.observe_output(
        cursor.session_id, after_seq=cursor.after_seq, limit=1
    )
    seq, offset, gap = _position(cursor, history)
    available = gap or any(
        len(chunk.text.encode("utf-8")) > (offset if index == 0 else 0)
        for index, chunk in enumerate(history.chunks)
    )
    return snapshot, available or seq < history.latest_seq and not history.chunks


def _read_items(
    manager: CommandProcessManager, request: ProcessReadRequest
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    per_session, extra = divmod(request.max_bytes, len(request.sessions))
    for index, cursor in enumerate(request.sessions):
        try:
            position = decode_cursor(cursor.session_id, cursor.cursor)
            item = page(manager, position, per_session + (index < extra))
            if not item["running"]:
                manager._complete(cursor.session_id)
            items.append(item)
        except (ValueError, OSError, RuntimeError) as exc:
            items.append(
                {"ok": False, "session_id": cursor.session_id, "error": str(exc)}
            )
    return items


def read_processes(
    manager: CommandProcessManager,
    request: ProcessReadRequest,
    *,
    cancel_requested: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Wait on runtime events, never on additional model-generated polling calls."""
    deadline = time.monotonic() + request.wait_ms / 1000
    changed = threading.Event()
    unsubscribe = manager.subscribe(changed.set)
    reason = "deadline"
    try:
        while True:
            # Clear before inspection so an event racing with inspection is retained.
            changed.clear()
            if cancel_requested is not None and cancel_requested():
                reason = "cancelled"
                break
            try:
                states = [
                    _state(manager, decode_cursor(cursor.session_id, cursor.cursor))
                    for cursor in request.sessions
                ]
            except (ValueError, OSError, RuntimeError):
                reason = "error"
                break
            if request.wait_ms == 0:
                reason = "snapshot"
                break
            finished = [snapshot.status != "running" for snapshot, _ in states]
            if (
                all(finished)
                or request.until == "output_or_completion"
                and any(finished)
            ):
                reason = "completed"
                break
            if request.until == "output_or_completion" and any(
                output for _, output in states
            ):
                reason = "output"
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # Cancellation callbacks are cooperative; wake periodically only for them.
            changed.wait(
                timeout=min(remaining, 0.05) if cancel_requested else remaining
            )
        items = _read_items(manager, request)
        if any(not item["ok"] for item in items):
            reason = "error" if reason != "cancelled" else reason
        result = {
            "ok": reason not in {"error", "cancelled"},
            "reason": reason,
            "items": items,
        }
        if reason == "cancelled":
            result.update(
                cancelled=True,
                error="Process read cancelled; processes were not stopped",
            )
        return result
    finally:
        unsubscribe()
