"""Derived session text, maintained inside the transcript's write transaction."""

from __future__ import annotations

import re
import sqlite3

from pydantic_core import from_json

_INSERT = (
    "INSERT INTO session_search(session_id, entry_id, title, text) VALUES (?, ?, ?, ?)"
)


def message_text(raw: bytes) -> str:
    """Extract visible user/assistant text, ignoring tools, metadata and images.

    Codec v1 templates are valid JSON whose only splices are image payloads.
    Reading the template avoids reconstructing large images we must discard.
    The same extraction accepts ordinary entry bytes before blob encoding.
    """
    value = from_json(raw)
    if not isinstance(value, dict) or value.get("kind") not in {
        "user",
        "assistant",
        "assistant_tool_calls",
    }:
        return ""
    message = value.get("message")
    if not isinstance(message, dict) or message.get("role") not in {
        "user",
        "assistant",
    }:
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            part["text"]
            for part in content
            if isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        )
    return ""


def insert_entry(
    connection: sqlite3.Connection, session_id: str, entry_id: str, raw: bytes
) -> None:
    """Index one newly inserted entry using its unmodified codec bytes."""
    text = message_text(raw)
    if text.strip():
        connection.execute(_INSERT, (session_id, entry_id, "", text))


def set_title(connection: sqlite3.Connection, session_id: str, title: object) -> None:
    """Replace the one title document, using an empty entry ID as its identity."""
    connection.execute(
        "DELETE FROM session_search WHERE session_id = ? AND entry_id = ''",
        (session_id,),
    )
    if isinstance(title, str) and title.strip():
        connection.execute(_INSERT, (session_id, "", title, ""))


def delete_entries(connection: sqlite3.Connection, session_id: str) -> None:
    """Remove transcript documents before a complete entry rewrite."""
    connection.execute(
        "DELETE FROM session_search WHERE session_id = ? AND entry_id != ''",
        (session_id,),
    )


def delete_session(connection: sqlite3.Connection, session_id: str) -> None:
    """Remove all documents before deleting the owning session."""
    connection.execute("DELETE FROM session_search WHERE session_id = ?", (session_id,))


def discard_tool_context(
    connection: sqlite3.Connection, session_id: str, first_seq: int
) -> None:
    """Remove legacy tool-injected messages after SQL provenance classification."""
    connection.execute(
        "DELETE FROM session_search WHERE session_id = ? AND entry_id IN "
        "(SELECT id FROM entries WHERE session_id = ? AND seq >= ? AND kind = 'tool_context')",
        (session_id, session_id, first_seq),
    )


def fork_entries(
    connection: sqlite3.Connection, source_id: str, target_id: str
) -> None:
    """Copy derived text without decoding shared transcript blobs again."""
    connection.execute(
        "INSERT INTO session_search(session_id, entry_id, title, text) "
        "SELECT ?, entry_id, title, text FROM session_search WHERE session_id = ? AND entry_id != ''",
        (target_id, source_id),
    )


def backfill(connection: sqlite3.Connection) -> None:
    """Rebuild from v6 rows atomically with the migration and schema version.

    An interrupt rolls back the entire upgrade. Rebuilding twice is safe, and
    each pass streams only candidate messages, never tool output or images.
    """
    connection.execute("DELETE FROM session_search")
    connection.execute(
        "INSERT INTO session_search(session_id, entry_id, title, text) "
        "SELECT id, '', title, '' FROM sessions WHERE title IS NOT NULL AND title != ''"
    )
    rows = connection.execute(
        "SELECT e.session_id, e.id, b.data FROM entries e JOIN blobs b ON b.sha256 = e.message_sha256 "
        "WHERE e.role IN ('user', 'assistant') AND e.kind IN ('user', 'assistant', 'assistant_tool_calls')"
    )
    connection.executemany(
        _INSERT,
        (
            (owner, entry_id, "", text)
            for owner, entry_id, raw in rows
            if (text := message_text(raw)).strip()
        ),
    )
    connection.execute("INSERT INTO session_search(session_search) VALUES ('optimize')")


def literal_query(text: str) -> str:
    """Match all literal words; punctuation and FTS operators have no syntax."""
    words = re.findall(r"[^\W_]+", text, flags=re.UNICODE)
    return " AND ".join('"' + word.replace('"', '""') + '"' for word in words) or '""'


def short_snippet(value: str | None) -> str | None:
    """Bound FTS's token excerpt even when one token is thousands of characters."""
    if value is None or len(value) <= 280:
        return value
    first_match = value.find("<mark>")
    start = max(0, first_match - 60)
    clipped = ("… " if start else "") + value[start : start + 260]
    # Do not leave half of a highlight delimiter at the boundary.
    clipped = re.sub(r"</?(?:m(?:a(?:r(?:k)?)?)?)?$", "", clipped)
    # Keep our delimiter balanced when clipping inside a highlighted word.
    if clipped.rfind("<mark>") > clipped.rfind("</mark>"):
        clipped += "</mark>"
    return clipped + " …"


# MATERIALIZED evaluates FTS auxiliaries while their MATCH cursor is active,
# before windowing chooses each session's best document. Titles weigh more.
MATCHES = """
WITH matches AS MATERIALIZED (
    SELECT session_id, rowid, bm25(session_search, 0, 0, 8, 1) AS score,
           snippet(session_search, -1, '<mark>', '</mark>', ' … ', 32) AS search_snippet
    FROM session_search WHERE session_search MATCH :fts
), ranked AS (
    SELECT *, row_number() OVER (PARTITION BY session_id ORDER BY score, rowid) AS position
    FROM matches
), best AS (SELECT * FROM ranked WHERE position = 1),
"""
