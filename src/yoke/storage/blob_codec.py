"""Exact JSON byte splicing with content-addressed binary image children.

The blob hash always identifies the reconstructed bytes. Only a codec row can
select decoding; URL strings never carry storage instructions.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass

# Match JSON string tokens, not text inside another string. Escaped base64 is
# deliberately left inline. Prefix bytes remain in the template verbatim.
_STRINGS = re.compile(rb'"(?:[^"\\]|\\.)*"')
_DATA = re.compile(rb'"(data:image/[^"\\]*;base64,)([A-Za-z0-9+/]*={0,2})"\Z')


@dataclass(frozen=True)
class EncodedBlob:
    """Prepared outside a write lock; insertion is one atomic operation."""

    sha256: str
    raw: bytes
    template: bytes
    refs: tuple[tuple[int, str], ...]
    images: tuple[tuple[str, bytes], ...]
    failures: int = 0

    def insert(
        self, connection: sqlite3.Connection, *, replace_inline: bool = False
    ) -> str:
        """Store verified bytes atomically; a failed splice stays entirely inline."""
        exists = connection.execute(
            "SELECT 1 FROM blobs WHERE sha256 = ?", (self.sha256,)
        ).fetchone()
        if exists and not replace_inline:
            return self.sha256
        # The parent may also be an upload. Its logical bytes and hash stay
        # unchanged; upload reads use the same eager decoder.
        for digest, data in self.images:
            connection.execute(
                "INSERT OR IGNORE INTO blobs VALUES (?, ?)", (digest, data)
            )
        connection.execute(
            "INSERT OR IGNORE INTO blobs VALUES (?, ?)", (self.sha256, self.raw)
        )
        if replace_inline and not self.refs:
            # Metadata staging may already have externalized canonical bytes
            # that the image decoder later rejects. Restore them inline.
            connection.execute(
                "DELETE FROM blob_refs WHERE parent_sha = ?", (self.sha256,)
            )
            connection.execute(
                "DELETE FROM blob_codecs WHERE sha256 = ?", (self.sha256,)
            )
            connection.execute(
                "UPDATE blobs SET data = ? WHERE sha256 = ?", (self.raw, self.sha256)
            )
        if self.refs:
            valid = all(
                connection.execute(
                    "SELECT data FROM blobs WHERE sha256 = ?", (digest,)
                ).fetchone()
                == (data,)
                for digest, data in self.images
            )
            if not valid:
                return self.sha256
            connection.execute(
                "UPDATE blobs SET data = ? WHERE sha256 = ?",
                (self.template, self.sha256),
            )
            connection.execute(
                "INSERT OR REPLACE INTO blob_codecs VALUES (?, 1, ?, ?)",
                (self.sha256, json.dumps(self.refs), len(self.raw)),
            )
            connection.executemany(
                "INSERT OR IGNORE INTO blob_refs VALUES (?, ?)",
                ((self.sha256, digest) for _, digest in self.refs),
            )
            if decode_blob(connection, self.sha256, self.template) != self.raw:
                # Verification is inside the transaction, before any caller
                # repoints entries or lets GC discard the original blob.
                connection.execute(
                    "DELETE FROM blob_refs WHERE parent_sha = ?", (self.sha256,)
                )
                connection.execute(
                    "DELETE FROM blob_codecs WHERE sha256 = ?", (self.sha256,)
                )
                connection.execute(
                    "UPDATE blobs SET data = ? WHERE sha256 = ?",
                    (self.raw, self.sha256),
                )
        return self.sha256


def prepare_blob(raw: bytes, *, inline_urls: set[str] | None = None) -> EncodedBlob:
    """Externalize only canonical base64 and verify the complete rebuilt hash."""
    parts: list[bytes] = []
    refs: list[tuple[int, str]] = []
    images: dict[str, bytes] = {}
    end = 0
    size = 0
    failures = 0
    inline = {json.dumps(url, ensure_ascii=False).encode() for url in inline_urls or ()}
    for token in _STRINGS.finditer(raw):
        if token.group() in inline:
            continue
        match = _DATA.fullmatch(token.group())
        if match is None:
            continue
        payload = match[2]
        try:
            data = base64.b64decode(payload, validate=True)
        except (ValueError, binascii.Error):
            failures += 1
            continue
        if base64.b64encode(data) != payload or not data:
            failures += 1
            continue
        start = token.start() + 1 + len(match[1])
        part = raw[end:start]
        parts.append(part)
        size += len(part)
        digest = hashlib.sha256(data).hexdigest()
        refs.append((size, digest))
        images[digest] = data
        end = start + len(payload)
    parts.append(raw[end:])
    template = b"".join(parts)
    digest = hashlib.sha256(raw).hexdigest()
    rebuilt = _rebuild(template, refs, images)
    if rebuilt != raw or hashlib.sha256(rebuilt).hexdigest() != digest:
        return EncodedBlob(digest, raw, raw, (), (), failures + len(refs))
    return EncodedBlob(
        digest, raw, template, tuple(refs), tuple(images.items()), failures
    )


def decode_blob(
    connection: sqlite3.Connection, digest: str | None, data: bytes | None
) -> bytes | None:
    """Eagerly reconstruct and verify a blob inside the caller's read snapshot."""
    if data is None or digest is None:
        return data
    try:
        codec = connection.execute(
            "SELECT codec, manifest FROM blob_codecs WHERE sha256 = ?", (digest,)
        ).fetchone()
    except sqlite3.OperationalError as exc:
        # Migration v2 indexes provenance before v4 creates the codec table.
        if str(exc) == "no such table: blob_codecs":
            return data
        raise
    if codec is None:
        return data
    if codec[0] != 1:
        raise ValueError(f"Unsupported blob codec: {codec[0]}")
    refs = json.loads(codec[1])
    images = {}
    for _, child in refs:
        if child not in images:
            row = connection.execute(
                "SELECT data FROM blobs WHERE sha256 = ?", (child,)
            ).fetchone()
            if row is None or hashlib.sha256(row[0]).hexdigest() != child:
                raise ValueError(f"Missing or corrupt image blob: {child}")
            images[child] = row[0]
    raw = _rebuild(data, refs, images)
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError(f"Corrupt entry blob: {digest}")
    return raw


def _rebuild(
    template: bytes,
    refs: list[tuple[int, str]] | tuple[tuple[int, str], ...],
    images: dict[str, bytes],
) -> bytes:
    parts = []
    previous = 0
    for offset, child in refs:
        if not previous <= offset <= len(template):
            raise ValueError("Invalid image splice offset")
        parts.extend((template[previous:offset], base64.b64encode(images[child])))
        previous = offset
    parts.append(template[previous:])
    return b"".join(parts)


def collect_blobs(connection: sqlite3.Connection, *, limit: int = 128) -> int:
    """Collect parents before children; subsequent batches free orphan images."""
    return connection.execute(
        "DELETE FROM blobs WHERE sha256 IN (SELECT sha256 FROM blobs "
        "WHERE NOT EXISTS (SELECT 1 FROM entries WHERE message_sha256 = blobs.sha256) "
        "AND NOT EXISTS (SELECT 1 FROM entries WHERE metadata_sha256 = blobs.sha256) "
        "AND NOT EXISTS (SELECT 1 FROM uploads WHERE sha256 = blobs.sha256) "
        "AND NOT EXISTS (SELECT 1 FROM blob_refs WHERE child_sha = blobs.sha256) LIMIT ?)",
        (limit,),
    ).rowcount
