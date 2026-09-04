"""Atomic file imports, bounded binary transfer, and byte-preserving export."""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import os
import secrets
import socket
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import Field

from yoke.mcp_server.execution.models import Request

MAX_FILE = 64 * 1024 * 1024
MAX_CHUNK = 2 * 1024 * 1024


class FileObject(Request):
    download_url: str
    file_id: str
    mime_type: str | None = None
    file_name: str | None = None


class Destination(Request):
    path: str = Field(min_length=1)
    sha256: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{64}$")
    expected_sha256: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{64}$")


class ImportFiles(Request):
    files: list[FileObject] = Field(min_length=1, max_length=8)
    destinations: list[Destination] = Field(min_length=1, max_length=8)


class WriteBinary(Destination):
    data_base64: str = Field(max_length=MAX_CHUNK * 4 // 3 + 4)
    transfer_id: str | None = None
    offset: int = Field(default=0, ge=0, le=MAX_FILE)
    final: bool = True


class ExportFile(Request):
    path: str
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=256 * 1024, ge=1, le=MAX_CHUNK)
    expected_sha256: str | None = None


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


class FileTransfers:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.Lock()
        self._uploads: dict[str, tuple[Path, Path, float, str | None]] = {}
        self._finished: dict[str, tuple[dict[str, Any], float, str]] = {}

    def path(self, value: str) -> Path:
        path = Path(value).expanduser()
        return (self.root / path).absolute() if not path.is_absolute() else path

    def _commit(self, temporary: Path, destination: Path, expected: str | None) -> None:
        if expected is None:
            os.link(temporary, destination)
            temporary.unlink()
        else:
            if destination.is_symlink() or digest(destination) != expected.lower():
                raise ValueError("Destination changed; expected hash does not match")
            os.replace(temporary, destination)

    def write(self, request: WriteBinary) -> dict[str, Any]:
        data = base64.b64decode(request.data_base64, validate=True)
        if len(data) > MAX_CHUNK or request.offset + len(data) > MAX_FILE:
            raise ValueError("Transfer exceeds chunk or file size limit")
        destination = self.path(request.path)
        with self._lock:
            self._prune()
            transfer = request.transfer_id
            fingerprint = hashlib.sha256(
                request.model_dump_json(exclude={"transfer_id"}).encode()
            ).hexdigest()
            if transfer in self._finished:
                result, _, previous = self._finished[transfer]
                if fingerprint != previous:
                    raise ValueError(
                        "Completed transfer retry must match its final request"
                    )
                return result
            if transfer is None:
                if request.offset != 0:
                    raise ValueError("New transfer must start at offset zero")
                if len(self._uploads) >= 8:
                    raise ValueError("Too many active transfers")
                fd, name = tempfile.mkstemp(
                    prefix=".yoke-upload-", dir=destination.parent
                )
                os.close(fd)
                transfer = secrets.token_urlsafe(24)
                self._uploads[transfer] = (
                    Path(name),
                    destination,
                    time.monotonic() + 900,
                    request.expected_sha256,
                )
            state = self._uploads.get(transfer)
            if state is None:
                raise ValueError("Unknown or expired transfer")
            temporary, target, _, expected = state
            if target != destination or expected != request.expected_sha256:
                raise ValueError(
                    "Transfer destination or overwrite precondition changed"
                )
            size = temporary.stat().st_size
            if request.offset > size:
                raise ValueError("Transfer has a gap; use next_offset")
            with temporary.open("r+b") as handle:
                handle.seek(request.offset)
                existing = handle.read(len(data))
                if existing and (
                    existing != data[: len(existing)]
                    or request.offset + len(data) > size
                ):
                    raise ValueError("Retry does not match previously accepted bytes")
                handle.seek(request.offset)
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            size = temporary.stat().st_size
            result: dict[str, Any] = {
                "ok": True,
                "transfer_id": transfer,
                "path": str(target),
                "next_offset": size,
                "bytes": size,
                "complete": False,
            }
            if request.final:
                sha = digest(temporary)
                if request.sha256 and sha != request.sha256.lower():
                    temporary.unlink(missing_ok=True)
                    del self._uploads[transfer]
                    raise ValueError("SHA-256 mismatch; destination was not changed")
                self._commit(temporary, target, expected)
                del self._uploads[transfer]
                result.update(complete=True, sha256=sha)
                self._finished[transfer] = (result, time.monotonic() + 900, fingerprint)
                if len(self._finished) > 512:
                    del self._finished[next(iter(self._finished))]
            return result

    def imports(self, request: ImportFiles) -> dict[str, Any]:
        if len(request.files) != len(request.destinations):
            raise ValueError("Each file needs one destination")
        if len({str(self.path(d.path)) for d in request.destinations}) != len(
            request.destinations
        ):
            raise ValueError("Import destinations must be distinct")
        results = []
        for source, target in zip(request.files, request.destinations):
            temporary: Path | None = None
            try:
                destination = self.path(target.path)
                fd, name = tempfile.mkstemp(
                    prefix=".yoke-import-", dir=destination.parent
                )
                temporary = Path(name)
                with os.fdopen(fd, "wb") as handle:
                    download(source.download_url, handle)
                    handle.flush()
                    os.fsync(handle.fileno())
                sha = digest(temporary)
                size = temporary.stat().st_size
                if target.sha256 and sha != target.sha256.lower():
                    raise ValueError("SHA-256 mismatch")
                with self._lock:
                    self._commit(temporary, destination, target.expected_sha256)
                results.append(
                    {
                        "ok": True,
                        "file_id": source.file_id,
                        "path": str(destination),
                        "sha256": sha,
                        "bytes": size,
                    }
                )
            except Exception:
                # HTTP exceptions may contain signed credentials. Never return URLs.
                results.append(
                    {
                        "ok": False,
                        "file_id": source.file_id,
                        "error": "Import failed: check URL expiry, destination, size and digest",
                    }
                )
            finally:
                if temporary:
                    temporary.unlink(missing_ok=True)
        return {"ok": all(r["ok"] for r in results), "items": results}

    def export(self, request: ExportFile) -> dict[str, Any]:
        path = self.path(request.path)
        if not path.is_file() or path.stat().st_size > MAX_FILE:
            raise ValueError("Export requires a regular file at most 64 MiB")
        # Hash and page one bounded snapshot, so concurrent edits cannot mix versions.
        with path.open("rb") as handle:
            raw = handle.read(MAX_FILE + 1)
        if len(raw) > MAX_FILE:
            raise ValueError("File exceeds export size limit")
        sha = hashlib.sha256(raw).hexdigest()
        if request.expected_sha256 and sha != request.expected_sha256:
            raise ValueError("File changed; restart export")
        if request.offset > len(raw):
            raise ValueError("Offset exceeds file size")
        data = raw[request.offset : request.offset + request.limit]
        end = request.offset + len(data)
        return {
            "ok": True,
            "path": str(path),
            "sha256": sha,
            "bytes": len(raw),
            "offset": request.offset,
            "next_offset": end if end < len(raw) else None,
            "data_base64": base64.b64encode(data).decode("ascii"),
        }

    def _prune(self) -> None:
        now = time.monotonic()
        for key, (path, _, expiry, _) in list(self._uploads.items()):
            if expiry < now:
                path.unlink(missing_ok=True)
                del self._uploads[key]
        self._finished = {
            key: value for key, value in self._finished.items() if value[1] > now
        }

    def close(self) -> None:
        with self._lock:
            for path, _, _, _ in self._uploads.values():
                path.unlink(missing_ok=True)
            self._uploads.clear()
            self._finished.clear()


def download(url: str, handle: Any) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("File download requires an HTTPS URL")
    addresses = socket.getaddrinfo(
        parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM
    )
    if not addresses or any(
        not ipaddress.ip_address(a[4][0]).is_global for a in addresses
    ):
        raise ValueError("Download host must resolve to public addresses")
    # Do not forward credentials across redirects or use environment proxies.
    with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as client:
        with client.stream(
            "GET",
            httpx.URL(url).copy_with(host=addresses[0][4][0]),
            headers={"Host": parsed.netloc},
            extensions={"sni_hostname": parsed.hostname},
        ) as response:
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError("Expected a direct file download")
            size = 0
            deadline = time.monotonic() + 60
            for data in response.iter_bytes(65536):
                size += len(data)
                if size > MAX_FILE or time.monotonic() > deadline:
                    raise ValueError("Download exceeds size or time limit")
                handle.write(data)
