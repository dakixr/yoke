"""Bounded, ephemeral result retention for the authenticated single-user service."""

from __future__ import annotations

import json
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class Entry:
    owner: str
    encoded: str
    expires: float


class ResultStore:
    def __init__(self, *, max_bytes: int = 64 * 1024 * 1024, ttl: int = 900) -> None:
        self.max_bytes = max_bytes
        self.ttl = ttl
        self._entries: dict[str, Entry] = {}
        self._lock = threading.Lock()

    def put(self, data: Any, *, owner: str = "service") -> dict[str, Any]:
        encoded = json.dumps(data, ensure_ascii=True, separators=(",", ":"))
        if len(encoded) > self.max_bytes:
            raise ValueError("Result exceeds retention budget; narrow the request")
        with self._lock:
            self._prune()
            if (
                sum(len(e.encoded) for e in self._entries.values()) + len(encoded)
                > self.max_bytes
            ):
                raise ValueError("Result store is full; retry after results expire")
            ref = secrets.token_urlsafe(24)
            self._entries[ref] = Entry(owner, encoded, time.monotonic() + self.ttl)
        return {
            "result_ref": ref,
            "bytes": len(encoded),
            "expires_in_seconds": self.ttl,
        }

    def read(
        self,
        ref: str,
        *,
        owner: str = "service",
        cursor: int = 0,
        limit: int = 16000,
        fields: list[str] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._prune()
            entry = self._entries.get(ref)
            if entry is None or entry.owner != owner:
                raise ValueError("Unknown or expired result handle")
            encoded = entry.encoded
        if fields:
            value = json.loads(encoded)
            if not isinstance(value, dict):
                raise ValueError("Field selection requires an object result")
            encoded = json.dumps(
                {key: value[key] for key in fields if key in value}, ensure_ascii=True
            )
        if cursor > len(encoded):
            raise ValueError("Cursor is beyond the retained result")
        end = min(len(encoded), cursor + limit)
        return {
            "ok": True,
            "result_ref": ref,
            "content": encoded[cursor:end],
            "cursor": cursor,
            "next_cursor": end if end < len(encoded) else None,
            "bytes": len(encoded),
            "complete": end == len(encoded),
        }

    def project(
        self,
        data: dict[str, Any],
        *,
        limit: int = 16000,
        fields: list[str] | None = None,
    ) -> dict[str, Any]:
        selected = {key: data[key] for key in fields if key in data} if fields else data
        encoded = json.dumps(selected, ensure_ascii=True)
        if len(encoded) <= limit:
            return selected
        retained = self.put(data)
        result = {
            "ok": data.get("ok", True),
            "truncated": True,
            **retained,
            "preview": "",
        }
        low, high = 0, min(len(encoded), limit)
        while low < high:
            middle = (low + high + 1) // 2
            result["preview"] = encoded[:middle]
            if len(json.dumps(result, ensure_ascii=True)) <= limit:
                low = middle
            else:
                high = middle - 1
        result["preview"] = encoded[:low]
        return result

    def _prune(self) -> None:
        now = time.monotonic()
        self._entries = {
            key: entry for key, entry in self._entries.items() if entry.expires > now
        }

    def close(self) -> None:
        with self._lock:
            self._entries.clear()
