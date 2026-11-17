"""Read-only database diagnosis, independent of migration and store startup."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from yoke.storage.blob_codec import decode_blob
from yoke.storage.schema import schema_version


@dataclass
class CheckResult:
    """Human-readable check results and a process exit decision."""

    path: Path
    lines: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """Whether every requested check succeeded."""
        return not self.problems


def check_database(path: Path, *, sample_size: int = 32) -> CheckResult:
    """Check one committed snapshot without creating a file or running migrations."""
    path = path.expanduser().absolute()
    result = CheckResult(path, lines=[f"Database: {path}"])
    try:
        with closing(
            sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        ) as connection:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            quick = [row[0] for row in connection.execute("PRAGMA quick_check")]
            if quick == ["ok"]:
                result.lines.append("quick_check: ok")
            else:
                result.problems.extend(f"quick_check: {line}" for line in quick)
            foreign = connection.execute("PRAGMA foreign_key_check").fetchall()
            if foreign:
                result.problems.extend(
                    f"foreign key violation: {row}" for row in foreign
                )
            else:
                result.lines.append("foreign keys: ok")
            found = connection.execute("PRAGMA user_version").fetchone()[0]
            supported = schema_version()
            result.lines.append(f"Schema: {found}, supported: {supported}")
            if found != supported:
                result.problems.append(
                    "Schema differs; upgrade Yoke to inspect a newer schema, or run yoke db migrate with the daemon stopped for an older one."
                )
            else:
                _check_content(connection, result, sample_size)
    except (OSError, sqlite3.Error, ValueError) as exc:
        result.problems.append(str(exc))
    result.lines.extend(f"Problem: {problem}" for problem in result.problems)
    result.lines.append(
        f"Result: {'ok' if result.ok else 'FAILED'} ({len(result.problems)} problems)"
    )
    return result


def _check_content(
    connection: sqlite3.Connection, result: CheckResult, sample_size: int
) -> None:
    # Prefer encoded parents so an arbitrary sample of image children cannot
    # accidentally leave every codec untested. Ordinary blobs are sampled too.
    rows = connection.execute(
        "SELECT b.sha256, b.data, c.logical_size, c.manifest FROM blobs b "
        "LEFT JOIN blob_codecs c ON c.sha256=b.sha256 "
        "ORDER BY c.sha256 IS NULL, b.sha256 LIMIT ?",
        (sample_size,),
    ).fetchall()
    for digest, data, logical_size, manifest in rows:
        try:
            raw = decode_blob(connection, digest, data)
            if raw is None or hashlib.sha256(raw).hexdigest() != digest:
                raise ValueError(f"Corrupt entry blob: {digest}")
            if logical_size is not None and len(raw) != logical_size:
                raise ValueError(f"Incorrect codec logical size: {digest}")
            if manifest is not None:
                children = {child for _, child in json.loads(manifest)}
                refs = {
                    row[0]
                    for row in connection.execute(
                        "SELECT child_sha FROM blob_refs WHERE parent_sha=?", (digest,)
                    )
                }
                if children != refs:
                    raise ValueError(f"Codec reference mismatch: {digest}")
        except Exception as exc:  # noqa: BLE001 - diagnosis must continue per blob
            result.problems.append(f"codec {digest}: {exc}")
    result.lines.append(f"codec reconstruction: sampled {len(rows)} blobs")
    # Events and queues deliberately have no FK: they may precede a session's
    # first save. Report references to absent sessions as orphan diagnostics.
    checks = {
        "entry parent": "SELECT count(*) FROM entries e WHERE e.parent_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM entries p WHERE p.session_id=e.session_id AND p.id=e.parent_id)",
        "session leaf": "SELECT count(*) FROM sessions s WHERE s.leaf_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM entries e WHERE e.session_id=s.id AND e.id=s.leaf_id)",
        "event session": "SELECT count(*) FROM events e WHERE NOT EXISTS (SELECT 1 FROM sessions s WHERE s.id=e.session_id)",
        "input session": "SELECT count(*) FROM inputs i WHERE NOT EXISTS (SELECT 1 FROM sessions s WHERE s.id=i.session_id)",
        "queue session": "SELECT count(*) FROM session_queues q WHERE NOT EXISTS (SELECT 1 FROM sessions s WHERE s.id=q.session_id)",
        "blob reference codec": "SELECT count(*) FROM blob_refs r WHERE NOT EXISTS (SELECT 1 FROM blob_codecs c WHERE c.sha256=r.parent_sha)",
    }
    for name, sql in checks.items():
        count = connection.execute(sql).fetchone()[0]
        if count:
            result.problems.append(f"orphan {name} refs: {count}")
    result.lines.append("orphan refs: checked")
