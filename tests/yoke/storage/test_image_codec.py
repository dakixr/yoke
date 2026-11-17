"""Exact entry images, reachability, idle conversion and approved resizing."""

from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path

import pytest
from PIL import Image

from yoke.agent.image_data import image_bytes_to_data_url
from yoke.agent.models import Message, TokenUsage
from yoke.session import SessionStore
from yoke.session.repository.table import SessionTable
from yoke.storage import Database
from yoke.storage.blob_codec import collect_blobs, decode_blob, prepare_blob
from yoke.storage.compactor import compact_batch, compact_database


def image_url(size=(32, 32), mode="RGB", color="red") -> str:
    buffer = io.BytesIO()
    Image.new(mode, size, color).save(buffer, format="PNG")
    return (
        "data:image/png;charset=utf-8;base64,"
        + base64.b64encode(buffer.getvalue()).decode()
    )


def quiet(database: Database) -> None:
    with database.write() as connection:
        connection.execute(
            "UPDATE sessions SET updated_at = '2000-01-01', created_at = '2000-01-01', last_user_at = NULL"
        )


def legacy(database: Database) -> None:
    """Recreate the pre-codec representation without changing logical bytes."""
    with database.write() as connection:
        rows = connection.execute(
            "SELECT sha256, yoke_blob_data(sha256, data) FROM blobs WHERE sha256 IN (SELECT message_sha256 FROM entries)"
        ).fetchall()
        connection.execute("DELETE FROM blob_refs")
        connection.execute("DELETE FROM blob_codecs")
        connection.execute("DELETE FROM blob_compaction")
        connection.executemany(
            "UPDATE blobs SET data = ? WHERE sha256 = ?",
            ((raw, sha) for sha, raw in rows),
        )


def test_codec_exact_prefix_offsets_noncanonical_and_forgery(tmp_path: Path) -> None:
    database = Database(tmp_path / "db", migrate=True)
    url = image_url()
    raw = json.dumps(
        {
            "a": url,
            "local_image": {"data_url": url},
            "forged": "yoke:image:sha256:anything",
            "noncanonical": "data:image/png;base64,Zh==",
            "escaped": 'text "data:image/png;base64,YQ=="',
        },
        ensure_ascii=False,
        indent=2,
    ).encode()
    prepared = prepare_blob(raw)
    assert len(prepared.refs) == 2
    assert len(prepared.images) == 1
    assert b";charset=utf-8;base64," in prepared.template
    assert b"Zh==" in prepared.template
    with database.write() as connection:
        sha = prepared.insert(connection)
        stored = connection.execute(
            "SELECT data FROM blobs WHERE sha256 = ?", (sha,)
        ).fetchone()[0]
        assert len(stored) < len(raw)
        assert decode_blob(connection, sha, stored) == raw
        assert hashlib.sha256(raw).hexdigest() == sha
        assert connection.execute("SELECT count(*) FROM blob_refs").fetchone()[0] == 1


def test_runtime_reads_metadata_forks_handoff_and_gc(tmp_path: Path) -> None:
    store = SessionStore(tmp_path, migrate=True)
    url = image_url()
    message = Message.user(
        [
            {"type": "image_url", "image_url": {"url": url}},
            {"type": "local_image", "path": "/missing", "data_url": url},
        ]
    )
    record = store.save("source", [message])
    entry = record.conversation_entries[0]
    entry.metadata = {
        "compaction_handoff": {"retained_messages": [message.model_dump(mode="json")]}
    }
    record.conversation_entries[0] = entry
    table = SessionTable(store.database)
    table.replace_entry_metadata(
        record, entry, [], expected_tree_rev=None, require_existing=True
    )
    loaded = store.load("source")
    assert loaded.conversation_entries[0] == entry
    assert list(table.iter_entries("source")) == loaded.conversation_entries
    assert table.read_entries("source", [entry.id]) == loaded.conversation_entries
    from yoke.session.handoff.reader import _elide_embedded_image_data

    elided = table.read_entries(
        "source", [entry.id], raw_filter=_elide_embedded_image_data
    )
    assert elided is not None
    msg = elided[0].message
    assert msg is not None and isinstance(msg.content, list)
    assert msg.content[0].type == "image_url"
    assert msg.content[0].image_url.url == "data:"
    fork = loaded.model_copy(deep=True, update={"id": "fork"})
    assert table.fork("source", fork)
    with store.database.write() as connection:
        assert (
            connection.execute(
                "SELECT count(DISTINCT child_sha) FROM blob_refs"
            ).fetchone()[0]
            == 1
        )
    table.delete("source")
    store.database.maintain(pages=0)
    assert store.load("fork").conversation_entries == loaded.conversation_entries
    table.delete("fork")
    with store.database.write() as connection:
        while collect_blobs(connection):
            pass
        assert connection.execute("SELECT count(*) FROM blobs").fetchone()[0] == 0


@pytest.mark.parametrize(
    "size,mode,color",
    [
        ((1600, 800), "RGB", "red"),
        ((1200, 1000), "RGBA", (0, 0, 0, 90)),
        ((10, 10), "RGBA", (0, 0, 0, 255)),
    ],
)
def test_image_limits_alpha_and_idempotence(size, mode, color) -> None:
    url = image_url(size, mode, color)
    output = image_bytes_to_data_url(base64.b64decode(url.split(",")[1]))
    data = base64.b64decode(output.split(",")[1])
    with Image.open(io.BytesIO(data)) as image:
        assert max(image.size) <= 1568
        assert image.width * image.height <= 1_150_000
        if color == (0, 0, 0, 90):
            assert image.format == "PNG"
            assert image.getchannel("A").getextrema() != (255, 255)
    assert image_bytes_to_data_url(data) == output


def test_compactor_resume_shared_hash_usage_and_active_owner(tmp_path: Path) -> None:
    store = SessionStore(tmp_path, migrate=True)
    record = store.save(
        "source",
        [
            Message(
                role="assistant",
                content="answer",
                usage=TokenUsage(raw={"large": "payload"}, input_tokens=3),
            )
        ],
    )
    # New writes strip raw even on full rewrites, while the public model keeps it.
    usage = store.load("source").messages[0].usage
    assert usage is not None and usage.raw == {}
    with store.database.write() as connection:
        entry = record.conversation_entries[0].model_dump(mode="json")
        entry["message"]["usage"]["raw"] = {"legacy": "payload"}
        raw = json.dumps(entry).encode()
        old = hashlib.sha256(raw).hexdigest()
        connection.execute("INSERT INTO blobs VALUES (?, ?)", (old, raw))
        connection.execute("UPDATE entries SET message_sha256 = ?", (old,))
        connection.execute("UPDATE sessions SET message_bytes = ?", (len(raw),))
    table = SessionTable(store.database)
    assert table.fork("source", record.model_copy(update={"id": "fork"}))
    quiet(store.database)
    with store.database.write() as connection:
        connection.execute(
            "UPDATE sessions SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now') WHERE id = 'fork'"
        )
    assert compact_batch(store.database).processed == 0
    quiet(store.database)
    with store.database.write() as connection:
        connection.execute(
            "INSERT INTO inputs(session_id,id,prompt,admission) VALUES ('fork','active','x', ?)",
            (json.dumps({"state": "promoted", "settled": False}),),
        )
    assert compact_batch(store.database).processed == 0
    with store.database.write() as connection:
        connection.execute("DELETE FROM inputs")
    signature = table.signature("source")
    result = compact_batch(store.database, limit=1)
    assert result.entries_rewritten == 2
    assert table.signature("source") != signature
    for owner in ("source", "fork"):
        loaded = store.load(owner)
        usage = loaded.messages[0].usage
        assert usage is not None and usage.raw == {}
        assert usage.input_tokens == 3
    assert compact_database(store.database).processed == 0


def test_compactor_metadata_images_and_transaction_rollback(
    tmp_path: Path, monkeypatch
) -> None:
    store = SessionStore(tmp_path, migrate=True)
    record = store.save("legacy", [Message.user("preserve text")])
    url = image_url((1600, 800))
    retained = Message.user([{"type": "local_image", "path": "/gone", "data_url": url}])
    metadata = {
        "compaction_handoff": {"retained_messages": [retained.model_dump(mode="json")]}
    }
    with store.database.write() as connection:
        connection.execute("UPDATE entries SET metadata = ?", (json.dumps(metadata),))
    quiet(store.database)
    # Stage the legacy override into its own rooted blob, then interrupt an
    # actual transform after insertion. References and completion marker must
    # roll back with the entry/session updates.
    assert compact_batch(store.database, limit=1).metadata_rewritten == 1
    from yoke.storage.blob_codec import EncodedBlob

    insert = EncodedBlob.insert

    def interrupted(self, connection, **kwargs):
        insert(self, connection, **kwargs)
        raise RuntimeError("simulated crash before repoint")

    with monkeypatch.context() as patch:
        patch.setattr(EncodedBlob, "insert", interrupted)
        with pytest.raises(RuntimeError, match="simulated crash"):
            compact_batch(store.database, limit=1)
    assert store.load("legacy").conversation_entries[0].metadata == metadata
    result = compact_database(store.database)
    assert result.images_reencoded == 1
    assert result.images_externalized == 1
    loaded = store.load("legacy")
    handoff = json.loads(json.dumps(loaded.conversation_entries[0].metadata))[
        "compaction_handoff"
    ]
    restored = Message.model_validate(handoff["retained_messages"][0])
    assert (
        isinstance(restored.content, list) and restored.content[0].type == "local_image"
    )
    actual = restored.content[0].data_url
    assert actual != url
    assert actual == image_bytes_to_data_url(base64.b64decode(url.split(",")[1]))
    assert loaded.messages == record.messages
    assert compact_database(store.database).processed == 0


def test_image_upload_dedup_and_invalid_data_stays_inline(tmp_path: Path) -> None:
    database = Database(tmp_path / "db", migrate=True)
    url = image_url()
    data = base64.b64decode(url.split(",")[1])
    digest = hashlib.sha256(data).hexdigest()
    with database.write() as connection:
        connection.execute("INSERT INTO blobs VALUES (?, ?)", (digest, data))
        connection.execute(
            "INSERT INTO uploads(id,name,mime,size,sha256,expires_at) VALUES ('u','image','image/png',?,?, '2999-01-01')",
            (len(data), digest),
        )
        prepared = prepare_blob(json.dumps({"url": url}).encode())
        parent = prepared.insert(connection)
        assert connection.execute("SELECT count(*) FROM blobs").fetchone()[0] == 2
        assert collect_blobs(connection) == 1
        assert connection.execute("SELECT sha256 FROM blobs").fetchall() == [(digest,)]
        connection.execute("DELETE FROM uploads")
        assert collect_blobs(connection) == 1
        assert parent != digest
    from yoke.storage.entry_transform import transform_entry

    invalid = b'{"message":{"role":"user","content":[{"type":"local_image","path":"/gone","data_url":"data:image/png;base64,YWJj"}]}}'
    transformed, images = transform_entry(invalid)
    encoded = prepare_blob(transformed, inline_urls=images.failed_urls)
    assert images.failures == 1
    assert encoded.refs == ()
    assert encoded.template == transformed
    with database.write() as connection:
        sha = prepare_blob(transformed).insert(connection)
        assert connection.execute(
            "SELECT 1 FROM blob_codecs WHERE sha256 = ?", (sha,)
        ).fetchone()
        encoded.insert(connection, replace_inline=True)
        assert (
            connection.execute(
                "SELECT 1 FROM blob_codecs WHERE sha256 = ?", (sha,)
            ).fetchone()
            is None
        )
        assert (
            connection.execute(
                "SELECT data FROM blobs WHERE sha256 = ?", (sha,)
            ).fetchone()[0]
            == transformed
        )


def test_import_and_full_rewrite_exclude_raw_usage_without_mutating_models(
    tmp_path: Path,
) -> None:
    from yoke.agent.models import ConversationEntry
    from yoke.session import SessionRecord
    from yoke.session.legacy.importer import verify_imported_session

    store = SessionStore(tmp_path, migrate=True)
    message = Message(
        role="assistant",
        content="kept",
        usage=TokenUsage(raw={"attribution": {"items": [1, 2, 3]}}, total_tokens=12),
    )
    entry = ConversationEntry(id="assistant", kind="assistant", message=message)
    record = SessionRecord(id="rewrite", conversation_entries=[entry], leaf_id=entry.id)
    table = SessionTable(store.database)
    assert table.insert_record(record)
    assert verify_imported_session(store, record) is None
    existing = store.load(record.id)
    entry.metadata = {
        "compaction_handoff": {"retained_messages": [message.model_dump(mode="json")]}
    }
    table.write_record(record, existing=existing, appended=None)
    assert message.usage is not None and message.usage.raw
    loaded = store.load(record.id).conversation_entries[0]
    assert loaded.message is not None and loaded.message.usage is not None
    assert loaded.message.usage.raw == {}
    assert loaded.message.usage.total_tokens == 12
    with store.database.read() as connection:
        raw = connection.execute(
            "SELECT yoke_blob_data(b.sha256,b.data) FROM entries e JOIN blobs b ON b.sha256=e.message_sha256"
        ).fetchone()[0]
        assert b'"raw"' not in raw


def test_codec_callback_does_not_keep_database_connections_alive(
    tmp_path: Path,
) -> None:
    import gc
    import weakref

    database = Database(tmp_path / "db", migrate=True)
    with database.read() as connection:
        reference = weakref.ref(connection)
    del connection
    del database
    gc.collect()
    assert reference() is None


def test_full_rewrite_keeps_old_oversized_images_eligible_for_idle_compaction(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path, migrate=True)
    url = image_url((1600, 800))
    message = Message.user([{"type": "image_url", "image_url": {"url": url}}])
    record = store.save("oversized", [message])
    # Entry encoding is exact. Rewrites must not falsely mark images from
    # older loaded sessions as already normalized by the ingress encoder.
    assert store.load("oversized").messages == record.messages
    quiet(store.database)
    result = compact_database(store.database)
    assert result.images_reencoded == 1
    assert result.images_externalized == 1
    loaded = store.load("oversized").messages[0]
    assert isinstance(loaded.content, list) and loaded.content[0].type == "image_url"
    assert loaded.content[0].image_url.url == image_bytes_to_data_url(
        base64.b64decode(url.split(",")[1])
    )


def test_repeated_saves_do_not_branch_on_omitted_diagnostic_usage(
    tmp_path: Path,
) -> None:
    store = SessionStore(tmp_path, migrate=True)
    messages = [
        Message.user("question"),
        Message(
            role="assistant",
            content="answer",
            usage=TokenUsage(raw={"payload": "large"}, total_tokens=7),
        ),
    ]
    first = store.save("same", messages)
    for _ in range(3):
        saved = store.save("same", messages)
        assert len(saved.conversation_entries) == 2
        assert saved.leaf_id == first.leaf_id
    usage = messages[1].usage
    assert usage is not None and usage.raw == {"payload": "large"}


@pytest.mark.parametrize("kind", ["truncated", "bomb", "unexpected"])
def test_poison_image_is_left_inline_and_never_retried(tmp_path, monkeypatch, kind):
    import struct
    import zlib

    raw_image = base64.b64decode(image_url().split(",")[1])
    if kind == "truncated":
        raw_image = raw_image[:50]
    elif kind == "bomb":
        # A valid IHDR CRC with a 100 million pixel size, without allocating it.
        ihdr = struct.pack(">IIBBBBB", 10_000, 10_000, 8, 2, 0, 0, 0)
        raw_image = (
            raw_image[:8]
            + struct.pack(">I", len(ihdr))
            + b"IHDR"
            + ihdr
            + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))
            + raw_image[33:]
        )
    url = "data:image/png;base64," + base64.b64encode(raw_image).decode()
    store = SessionStore(tmp_path, migrate=True)
    record = store.save(
        "poison", [Message.user([{"type": "image_url", "image_url": {"url": url}}])]
    )
    legacy(store.database)
    quiet(store.database)
    if kind == "unexpected":

        def fail(_raw):
            raise RuntimeError("unexpected decoder error")

        monkeypatch.setattr("yoke.storage.compactor.transform_entry", fail)
    result = compact_batch(store.database)
    assert result.failures_inline == 1
    assert result.processed == 1
    assert store.load("poison").conversation_entries == record.conversation_entries
    with store.database.read() as connection:
        assert connection.execute("SELECT count(*) FROM blob_codecs").fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM blob_compaction"
        ).fetchone() == (1,)
    with store.database.read() as connection:
        assert connection.execute(
            "SELECT failures FROM blob_compaction_failures"
        ).fetchall() == [(1,)]
    store.database.close()
    store = SessionStore(tmp_path, migrate=True)
    assert compact_batch(store.database).processed == 0


@pytest.mark.parametrize("failure", ["crash", "timeout"])
def test_native_decoder_failure_is_contained(tmp_path, monkeypatch, failure):
    import subprocess
    from yoke.storage import image_worker

    url = image_url()
    store = SessionStore(tmp_path, migrate=True)
    record = store.save(
        "decoder", [Message.user([{"type": "image_url", "image_url": {"url": url}}])]
    )
    legacy(store.database)
    quiet(store.database)
    calls = []

    def fail_decoder(argv, **kwargs):
        calls.append((argv, kwargs["timeout"]))
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        raise subprocess.CalledProcessError(-11, argv)

    monkeypatch.setattr(image_worker.subprocess, "run", fail_decoder)
    result = compact_batch(store.database)
    assert result.failures_inline == 1
    assert store.load("decoder").conversation_entries == record.conversation_entries
    assert calls[0][1] == 5
    assert compact_batch(store.database).processed == 0
    assert len(calls) == 1
