"""Atomic binary transfer, download validation, and native media contracts."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
from pathlib import Path

import pytest
from PIL import Image

from yoke.mcp_server.config import MCPServerConfig
from yoke.mcp_server.results.encoding import encode
from yoke.mcp_server.results.store import ResultStore
from yoke.mcp_server.server import create_service
from yoke.mcp_server.transfers.files import FileTransfers, ImportFiles, download

from .helpers import memory_client, structured


def test_binary_chunks_retry_digest_and_create_only(tmp_path: Path) -> None:
    raw = b"\x00\xffbinary" * 500
    sha = hashlib.sha256(raw).hexdigest()

    async def scenario() -> None:
        service = create_service(MCPServerConfig(root=tmp_path))
        async with memory_client(service) as client:
            first_args = {
                "path": "image.bin",
                "data_base64": base64.b64encode(raw[:1000]).decode(),
                "final": False,
            }
            first = structured(await client.call_tool("write_binary_file", first_args))
            assert first["ok"]
            assert not (tmp_path / "image.bin").exists()
            retry = structured(
                await client.call_tool(
                    "write_binary_file",
                    {**first_args, "transfer_id": first["transfer_id"]},
                )
            )
            assert retry == first
            mismatch = await client.call_tool(
                "write_binary_file",
                {
                    **first_args,
                    "transfer_id": first["transfer_id"],
                    "data_base64": "AAAA",
                },
            )
            assert mismatch.is_error
            last_args = {
                "path": "image.bin",
                "transfer_id": first["transfer_id"],
                "offset": 1000,
                "data_base64": base64.b64encode(raw[1000:]).decode(),
                "sha256": sha,
            }
            last = structured(await client.call_tool("write_binary_file", last_args))
            assert last["ok"] and last["complete"]
            assert (tmp_path / "image.bin").read_bytes() == raw
            assert (
                structured(await client.call_tool("write_binary_file", last_args))
                == last
            )
            existing = await client.call_tool(
                "write_binary_file", {"path": "image.bin", "data_base64": "AAAA"}
            )
            assert existing.is_error
            assert (tmp_path / "image.bin").read_bytes() == raw
            bad_digest = await client.call_tool(
                "write_binary_file",
                {"path": "new", "data_base64": "AAAA", "sha256": "0" * 64},
            )
            assert bad_digest.is_error and not (tmp_path / "new").exists()
            invalid = await client.call_tool(
                "write_binary_file", {"path": "new", "data_base64": "!invalid"}
            )
            assert invalid.is_error
            exported = structured(
                await client.call_tool(
                    "export_file", {"path": "image.bin", "limit": 1000}
                )
            )
            assert exported["sha256"] == sha
            assert base64.b64decode(exported["data_base64"]) == raw[:1000]
            (tmp_path / "image.bin").write_bytes(b"changed")
            changed = await client.call_tool(
                "export_file",
                {"path": "image.bin", "offset": 1000, "expected_sha256": sha},
            )
            assert changed.is_error
        assert not list(tmp_path.glob(".yoke-upload-*"))

    asyncio.run(scenario())


def test_import_files_never_returns_signed_urls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_download(url, handle):
        if "fail" in url:
            raise ValueError(url)
        handle.write(b"original bytes")

    monkeypatch.setattr("yoke.mcp_server.transfers.files.download", fake_download)
    transfers = FileTransfers(tmp_path)
    request = ImportFiles.model_validate(
        {
            "files": [
                {"download_url": "https://host/file?secret=secret", "file_id": "one"},
                {"download_url": "https://host/fail?secret=secret", "file_id": "two"},
            ],
            "destinations": [{"path": "one"}, {"path": "two"}],
        }
    )
    result = transfers.imports(request)
    assert (tmp_path / "one").read_bytes() == b"original bytes"
    assert not (tmp_path / "two").exists()
    assert not result["ok"]
    assert "secret" not in json.dumps(result)
    assert not list(tmp_path.glob(".yoke-import-*"))
    transfers.close()


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/file",
        "https://127.0.0.1/file",
        "https://user:password@example.com/file",
    ],
)
def test_import_rejects_nonpublic_or_credential_urls(url: str) -> None:
    with pytest.raises(ValueError):
        download(url, io.BytesIO())


def test_native_images_preserve_bytes_and_reject_mime_mismatch() -> None:
    buffer = io.BytesIO()
    Image.new("RGB", (3, 3), "red").save(buffer, format="PNG")
    data = base64.b64encode(buffer.getvalue()).decode()
    store = ResultStore()
    result = {
        "ok": True,
        "content": [{"type": "image", "mimeType": "image/png", "data": data}],
    }
    encoded = encode(result, store)
    assert encoded.content[1].type == "image"
    assert encoded.content[1].data == data
    assert data not in json.dumps(encoded.structured_content)
    result["content"][0]["mimeType"] = "image/jpeg"  # type: ignore[index]
    with pytest.raises(ValueError, match="MIME"):
        encode(result, store)


def test_result_store_expiry_ownership_and_capacity() -> None:
    store = ResultStore(max_bytes=100, ttl=30)
    ref = store.put({"value": 1}, owner="alice")["result_ref"]
    with pytest.raises(ValueError, match="Unknown"):
        store.read(ref, owner="bob")
    assert json.loads(store.read(ref, owner="alice")["content"]) == {"value": 1}
    with pytest.raises(ValueError, match="budget"):
        store.put({"large": "x" * 100})
    expired = ResultStore(ttl=0)
    ref = expired.put({"value": 1})["result_ref"]
    with pytest.raises(ValueError, match="expired"):
        expired.read(ref)
