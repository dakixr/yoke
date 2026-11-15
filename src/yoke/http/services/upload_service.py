"""Daemon-owned prompt attachment uploads."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import secrets
import shutil
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import UploadFile
from pydantic import BaseModel

from yoke.agent.attachments import attachment_mime, attachment_name, validate_attachment
from yoke.http.errors import ApiError, session_not_found
from yoke.http.models.upload import UploadInfo
from yoke.session import SessionStore

MAX_UPLOAD_BYTES = 20_971_520
UPLOAD_TTL = timedelta(days=1)


class StoredUpload(BaseModel):
    id: str
    session_id: str | None = None
    name: str
    mime: str
    size: int
    sha256: str
    expires_at: str
    durable: bool = False


class UploadService:
    """Store and resolve bounded local prompt attachments."""

    def __init__(self, store: SessionStore) -> None:
        self.store = store
        self.directory = store.directory / "uploads"
        self._import_files()
        with store.database.read() as connection:
            ids = connection.execute(
                "SELECT id FROM uploads WHERE durable = 1 AND mime NOT LIKE 'image/%'"
            ).fetchall()
            for (upload_id,) in ids:
                record = self._load_uri(f"yoke-upload://{upload_id}")
                self._materialize(record)

    def _import_files(self) -> None:
        """Move Phase 1 upload records into SQLite, keeping native tool paths."""
        if not self.directory.is_dir():
            return
        for metadata in self.directory.glob("upl_*/metadata.json"):
            try:
                payload = json.loads(metadata.read_bytes())
                upload_id = metadata.parent.name
                if not re.fullmatch(r"upl_[0-9a-f]{24}", upload_id):
                    continue
                name = attachment_name(payload["name"])
                data = (metadata.parent / "files" / name).read_bytes()
                record = StoredUpload.model_validate(
                    {
                        **payload,
                        "id": upload_id,
                        "name": name,
                        "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                )
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if record.session_id is not None and not self.store.exists(
                record.session_id
            ):
                continue
            with self.store.database.write() as connection:
                connection.execute(
                    "INSERT OR IGNORE INTO blobs VALUES (?, ?)", (record.sha256, data)
                )
                connection.execute(
                    "INSERT OR IGNORE INTO uploads (id, session_id, name, mime, size, sha256, expires_at, durable) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record.id,
                        record.session_id,
                        record.name,
                        record.mime,
                        record.size,
                        record.sha256,
                        record.expires_at,
                        int(record.durable),
                    ),
                )
            metadata.unlink(missing_ok=True)

    async def create(
        self,
        upload: UploadFile,
        *,
        session_id: str | None,
    ) -> UploadInfo:
        self.gc_expired_orphans()
        if session_id is not None and not self.store.exists(session_id):
            raise session_not_found()
        name = attachment_name(upload.filename or "attachment")
        mime = (
            upload.content_type
            or mimetypes.guess_type(name)[0]
            or "application/octet-stream"
        )
        try:
            mime = attachment_mime(mime)
        except ValueError as exc:
            raise ApiError(400, "unsupported_attachment", str(exc)) from exc
        upload_id = f"upl_{secrets.token_hex(12)}"
        data = bytearray()
        while chunk := await upload.read(1024 * 1024):
            if len(data) + len(chunk) > MAX_UPLOAD_BYTES:
                raise ApiError(
                    413, "attachment_too_large", "Attachment exceeds the server limit."
                )
            data.extend(chunk)
        payload = bytes(data)
        if mime.startswith("image/"):
            try:
                validate_attachment(payload, mime)
            except ValueError as exc:
                raise ApiError(400, "invalid_image", str(exc)) from exc
        expires = datetime.now(UTC) + UPLOAD_TTL
        record = StoredUpload(
            id=upload_id,
            session_id=session_id,
            name=name,
            mime=mime,
            size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            expires_at=expires.isoformat(),
        )
        with self.store.database.write() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO blobs VALUES (?, ?)", (record.sha256, payload)
            )
            connection.execute(
                "INSERT INTO uploads (id, session_id, name, mime, size, sha256, expires_at, durable) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.id,
                    record.session_id,
                    record.name,
                    record.mime,
                    record.size,
                    record.sha256,
                    record.expires_at,
                    int(record.durable),
                ),
            )
        return self._info(record)

    def resolve(
        self,
        uri: str,
        *,
        session_id: str,
        name: str,
        mime: str,
    ) -> Path:
        record = self._load_uri(uri)
        if not record.durable and _expired(record.expires_at):
            self._delete(record.id)
            raise ApiError(404, "upload_not_found", "Upload was not found.")
        if record.session_id is not None and record.session_id != session_id:
            raise ApiError(
                403,
                "attachment_session_mismatch",
                "Upload belongs to another session.",
            )
        if record.name != name or record.mime != mime:
            raise ApiError(
                409,
                "attachment_identity_conflict",
                "Attachment metadata does not match the stored upload.",
            )
        return self._materialize(record)

    def _materialize(self, record: StoredUpload) -> Path:
        # Native file tools need a path, also embedded in existing provider
        # messages. This is a disposable projection, never a persistence source.
        path = self.directory / record.id / "files" / record.name
        with self.store.database.read() as connection:
            row = connection.execute(
                "SELECT data FROM blobs WHERE sha256 = ?", (record.sha256,)
            ).fetchone()
        if row is None:
            raise ApiError(404, "upload_not_found", "Upload was not found.")
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            try:
                handle.write(row[0])
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        try:
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return path.resolve()

    def pin(
        self,
        uri: str,
        *,
        session_id: str,
        name: str,
        mime: str,
    ) -> None:
        """Make one referenced upload durable for the lifetime of session state."""
        with self.store.database.write() as connection:
            self.resolve(uri, session_id=session_id, name=name, mime=mime)
            record = self._load_uri(uri)
            connection.execute(
                "UPDATE uploads SET session_id = ?, durable = 1 WHERE id = ?",
                (session_id, record.id),
            )

    def delete_bound_upload(self, uri: str, *, session_id: str) -> bool:
        """Delete one upload only when it is bound to the requesting session."""
        try:
            record = self._load_uri(uri)
        except ApiError as exc:
            if exc.status_code == 404:
                return False
            raise
        if record.session_id != session_id:
            return False
        self._delete(record.id)
        return True

    def validate_reference(
        self,
        uri: str,
        *,
        session_id: str,
        name: str,
        mime: str,
    ) -> None:
        self.resolve(uri, session_id=session_id, name=name, mime=mime)

    def _load_uri(self, uri: str) -> StoredUpload:
        prefix = "yoke-upload://"
        if not uri.startswith(prefix):
            raise ApiError(
                400,
                "invalid_attachment_uri",
                "Prompt attachment URI must reference a Yoke upload.",
            )
        upload_id = uri[len(prefix) :]
        if not re.fullmatch(r"upl_[0-9a-f]{24}", upload_id):
            raise ApiError(400, "invalid_attachment_uri", "Invalid upload URI.")
        with self.store.database.read() as connection:
            row = connection.execute(
                "SELECT id, session_id, name, mime, size, sha256, expires_at, durable "
                "FROM uploads WHERE id = ?",
                (upload_id,),
            ).fetchone()
        if row is None:
            raise ApiError(404, "upload_not_found", "Upload was not found.")
        return StoredUpload.model_validate(
            dict(zip(StoredUpload.model_fields, row, strict=True))
        )

    def gc_expired_orphans(self) -> int:
        """Remove expired uploads that were never referenced by durable state."""
        with self.store.database.write() as connection:
            ids = connection.execute(
                "SELECT id, expires_at FROM uploads WHERE durable = 0"
            ).fetchall()
            expired = [upload_id for upload_id, expiry in ids if _expired(expiry)]
            for upload_id in expired:
                self._delete(upload_id)
        return len(expired)

    def _delete(self, upload_id: str) -> None:
        with self.store.database.write() as connection:
            connection.execute("DELETE FROM uploads WHERE id = ?", (upload_id,))
        shutil.rmtree(self.directory / upload_id, ignore_errors=True)

    @staticmethod
    def _info(record: StoredUpload) -> UploadInfo:
        return UploadInfo(
            id=record.id,
            uri=f"yoke-upload://{record.id}",
            name=record.name,
            mime=record.mime,
            size=record.size,
            expires_at=record.expires_at,
        )


def _expired(value: str) -> bool:
    try:
        return datetime.fromisoformat(value) <= datetime.now(UTC)
    except ValueError:
        return True
