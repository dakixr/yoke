#!/usr/bin/env python3
"""Read-only inventory of on-disk Yoke session formats."""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path


def classify(path: Path) -> str:
    try:
        with path.open(encoding="utf-8") as handle:
            lines = (line.strip() for line in handle)
            first = next((line for line in lines if line), "")
    except (OSError, UnicodeDecodeError):
        return "unreadable"
    try:
        payload = json.loads(first)
    except json.JSONDecodeError:
        return "invalid"
    if isinstance(payload, dict) and payload.get("type") == "yoke_session":
        return f"canonical-header-v{payload.get('version')}"
    if isinstance(payload, dict) and payload.get("type") == "session_stream":
        return "legacy-session-stream"
    if isinstance(payload, dict) and payload.get("type") == "session_record":
        return "legacy-session-record"
    if isinstance(payload, dict) and "kind" in payload and "message" in payload:
        return "legacy-entry-jsonl"
    return "legacy-json-record"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("directories", nargs="*", type=Path)
    args = parser.parse_args()
    directories = args.directories or [
        Path.home() / ".yoke" / "sessions",
        Path("/home/dakixr/dev"),
    ]
    counts: Counter[str] = Counter()
    for directory in directories:
        if not directory.exists():
            continue
        for path in directory.rglob("*.jsonl"):
            if ".yoke" not in path.parts and directory == Path("/home/dakixr/dev"):
                continue
            counts[classify(path)] += 1
    for format_name in sorted(counts):
        print(f"{format_name}: {counts[format_name]}")
    print(f"total: {sum(counts.values())}")


if __name__ == "__main__":
    main()
