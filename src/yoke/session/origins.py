"""Creating-client provenance, independent of transcript and later attachments."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Literal, cast

from pydantic_core import from_json

type SessionOrigin = Literal["cli", "web", "acp", "sdk", "unknown"]
_KNOWN = frozenset({"cli", "web", "acp", "sdk"})


def infer_origin(metadata: Mapping[str, object]) -> SessionOrigin:
    """Use explicit origin or creating-client fields; conflicting evidence is unknown.

    Titles, workspace paths, model choices and transcript text are not evidence.
    Attachments made later do not identify the client that created a session.
    """
    evidence = set()
    origin = metadata.get("origin")
    if isinstance(origin, str) and origin in _KNOWN:
        evidence.add(origin)
    for key in ("client", "client_session"):
        client = metadata.get(key)
        if isinstance(client, dict):
            kind = client.get("kind")
            if isinstance(kind, str) and kind in _KNOWN:
                evidence.add(kind)
    return cast(SessionOrigin, evidence.pop()) if len(evidence) == 1 else "unknown"


def repair_origins(connection: sqlite3.Connection) -> None:
    """Repair old imports without relabeling native daemon session creation.

    The old importer defaulted to cli. import_hold identifies untouched imports;
    a resumed import may have cleared it, but has no native session.created
    event. Only explicit creating-client evidence can refine these candidates.
    The SQLite importer never retained arbitrary JSONL header/index fields.
    """
    candidates = connection.execute(
        "SELECT id FROM sessions s WHERE origin = 'cli' AND (import_hold = 1 OR "
        "NOT EXISTS (SELECT 1 FROM events e WHERE e.session_id = s.id AND e.type = 'session.created'))"
    ).fetchall()
    for (session_id,) in candidates:
        created = connection.execute(
            "SELECT data FROM events WHERE session_id = ? AND type = 'session.created' ORDER BY seq LIMIT 1",
            (session_id,),
        ).fetchone()
        origin = infer_origin(from_json(created[0])) if created else "unknown"
        connection.execute(
            "UPDATE sessions SET origin = ?, rev = rev + 1 WHERE id = ?",
            (origin, session_id),
        )
