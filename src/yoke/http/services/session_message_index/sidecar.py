"""Durable sidecar serialization for HTTP session message indexes."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic_core import from_json
from pydantic_core import to_json

from yoke.http.services.session_message_index.models import INDEX_VERSION
from yoke.http.services.session_message_index.models import MessageIndexSnapshot

if TYPE_CHECKING:
    from yoke.http.services.session_message_index import SessionMessageIndex


def sidecar_path(host: SessionMessageIndex, session_id: str) -> Path:
    return host.store.directory / "read-index" / f"{session_id}.json"


def load_sidecar(
    host: SessionMessageIndex, session_id: str
) -> MessageIndexSnapshot | None:
    path = sidecar_path(host, session_id)
    try:
        payload = from_json(path.read_bytes())
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != INDEX_VERSION:
        return None
    raw_entries = payload.get("entries")
    if not isinstance(raw_entries, dict):
        return None
    try:
        entries = {
            entry_id: value
            for entry_id, value in raw_entries.items()
            if isinstance(entry_id, str) and isinstance(value, list) and len(value) == 6
        }
        if len(entries) != len(raw_entries):
            return None
        return MessageIndexSnapshot(
            source_size=int(payload["sourceSize"]),
            source_mtime_ns=int(payload["sourceMtimeNs"]),
            indexed_size=int(payload["indexedSize"]),
            prefix_hash=str(payload["prefixHash"]),
            leaf_id=payload.get("leafID"),
            entries=entries,
        )
    except (KeyError, TypeError, ValueError, IndexError):
        return None


def write_sidecar(
    host: SessionMessageIndex,
    session_id: str,
    snapshot: MessageIndexSnapshot,
) -> None:
    path = sidecar_path(host, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": INDEX_VERSION,
        "sourceSize": snapshot.source_size,
        "sourceMtimeNs": snapshot.source_mtime_ns,
        "indexedSize": snapshot.indexed_size,
        "prefixHash": snapshot.prefix_hash,
        "leafID": snapshot.leaf_id,
        "entries": dict(snapshot.entries),
    }
    tmp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        tmp.write_bytes(to_json(payload))
        tmp.replace(path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def link_sidecar(
    host: SessionMessageIndex, source_session_id: str, target_session_id: str
) -> bool:
    """Seed a fork from an existing immutable sidecar inode in O(1)."""
    source = sidecar_path(host, source_session_id)
    target = sidecar_path(host, target_session_id)
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
    try:
        if not source.is_file():
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        os.link(source, temporary)
        temporary.replace(target)
        return True
    except OSError:
        return False
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
