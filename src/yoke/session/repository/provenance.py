"""Derive public topology without changing the immutable entry encoding."""

from __future__ import annotations

import sqlite3

from yoke.agent.models import ConversationEntry
from yoke.agent.tool_context import legacy_tool_context_entry_ids
from yoke.session.repository.rows import decode_entry


def classify_legacy_entries(
    connection: sqlite3.Connection,
    *,
    session_id: str | None = None,
    first_seq: int = 0,
) -> None:
    """Index pre-provenance image injections once, at insertion or migration.

    The loader continues to return the original bytes and kind. Only HTTP's
    topology uses the derived kind, matching its old provenance repair.
    """
    where = " AND child.session_id = ? AND child.seq >= ?" if session_id else ""
    parameters = (session_id, first_seq) if session_id else ()
    candidates = connection.execute(
        "SELECT child.session_id, child.id FROM entries child JOIN entries parent "
        "ON parent.session_id = child.session_id AND parent.id = child.parent_id "
        "WHERE child.kind = 'user' AND parent.kind = 'tool_result'" + where,
        parameters,
    ).fetchall()
    for owner, candidate in candidates:
        chain: list[ConversationEntry] = []
        seen: set[str] = set()
        current = candidate
        while current is not None and current not in seen:
            seen.add(current)
            row = connection.execute(
                "SELECT yoke_blob_data(blobs.sha256, blobs.data), entries.metadata FROM entries JOIN blobs "
                "ON blobs.sha256 = entries.message_sha256 "
                "WHERE session_id = ? AND id = ?",
                (owner, current),
            ).fetchone()
            if row is None:
                break
            message, metadata = row
            if connection.execute("PRAGMA user_version").fetchone()[0] >= 4:
                override = connection.execute(
                    "SELECT yoke_blob_data(b.sha256, b.data) FROM entries e "
                    "JOIN blobs b ON b.sha256 = e.metadata_sha256 "
                    "WHERE e.session_id = ? AND e.id = ?",
                    (owner, current),
                ).fetchone()
                if override is not None:
                    metadata = override[0].decode()
            entry = decode_entry(message, metadata)
            chain.append(entry)
            if current != candidate and entry.kind == "user":
                break
            current = entry.parent_id
        if candidate in legacy_tool_context_entry_ids(chain):
            connection.execute(
                "UPDATE entries SET kind = 'tool_context' WHERE session_id = ? AND id = ?",
                (owner, candidate),
            )
