"""Streaming physical and logical database size accounting."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from yoke.storage.admin import database_info
from yoke.storage.database import Database


def field_sizes(value: Any) -> tuple[int, int]:
    """Return image-string and usage JSON bytes, without double counting."""
    images = usage = 0
    if isinstance(value, list):
        for child in value:
            image_size, usage_size = field_sizes(child)
            images += image_size
            usage += usage_size
    elif isinstance(value, dict):
        for key, child in value.items():
            if key == "usage" and isinstance(child, dict):
                usage += len(
                    json.dumps(
                        child, separators=(",", ":"), ensure_ascii=False
                    ).encode()
                )
            elif (
                key in {"url", "data_url"}
                and isinstance(child, str)
                and child.startswith("data:")
            ):
                images += len(child.encode())
            else:
                image_size, usage_size = field_sizes(child)
                images += image_size
                usage += usage_size
    return images, usage


def database_stats(database: Database) -> list[str]:
    """Count logical entry bytes per reference; physical sizes include indexes."""
    lines = database_info(database).lines()
    kinds: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    with database.stream_read() as connection:
        lines.append("Physical tables/indexes (bytes):")
        for name, size in connection.execute(
            "SELECT name, sum(pgsize) FROM dbstat GROUP BY name ORDER BY sum(pgsize) DESC"
        ):
            lines.append(f"  {name}: {size}")
        for kind, raw, metadata in connection.execute(
            "SELECT e.kind, yoke_blob_data(b.sha256, b.data), "
            "coalesce(CAST(yoke_blob_data(e.metadata_sha256, (SELECT data FROM blobs WHERE sha256 = e.metadata_sha256)) AS TEXT), e.metadata) "
            "FROM entries e JOIN blobs b ON b.sha256 = e.message_sha256"
        ):
            value = json.loads(raw)
            if metadata is not None:
                value["metadata"] = json.loads(metadata)
            logical = json.dumps(
                value, separators=(",", ":"), ensure_ascii=False
            ).encode()
            images, usage = field_sizes(value)
            sizes = kinds[kind]
            for index, amount in enumerate(
                (1, images, usage, len(logical) - images - usage)
            ):
                sizes[index] += amount
        unique, binary = connection.execute(
            "SELECT count(*), coalesce(sum(length(data)), 0) FROM blobs WHERE sha256 IN (SELECT child_sha FROM blob_refs)"
        ).fetchone()
    lines.append("Logical entries by kind (references, images, usage, other bytes):")
    for kind, sizes in sorted(kinds.items()):
        lines.append(f"  {kind}: " + ", ".join(map(str, sizes)))
    lines.append(f"External image blobs: {unique}, binary bytes: {binary}")
    return lines
