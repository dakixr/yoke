"""Daemon-owned prompt attachment uploads."""

from __future__ import annotations

from datetime import UTC
from datetime import datetime
from datetime import timedelta
import mimetypes
from pathlib import Path
import secrets
import shutil

from fastapi import UploadFile
from pydantic import BaseModel

from yoke.agent.multimodal import IMAGE_EXTENSIONS
from yoke.http.errors import ApiError
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
    path: str
    expires_at: str
    durable: bool = False


class UploadService:
    """Store and resolve bounded local prompt attachments."""

    def __init__(self, store: SessionStore) -> None:
        self.store = store
        self.directory = store.directory / "uploads"

    async def create(
        self,
        upload: UploadFile,
        *,
        session_id: str | None,
    ) -> UploadInfo:
        self.gc_expired_orphans()
        if session_id is not None and not self.store.exists(session_id):
            raise ApiError(404, "session_not_found", "Session was not found.")
        name = Path(upload.filename or "attachment").name
        suffix = Path(name).suffix.lower()
        mime = upload.content_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
        if not mime.startswith("image/") or suffix not in IMAGE_EXTENSIONS:
            raise ApiError(
                400,
                "unsupported_attachment",
                "This daemon version accepts image prompt attachments only.",
            )
        upload_id = f"upl_{secrets.token_hex(12)}"
        target_dir = self.directory / upload_id
        target_dir.mkdir(parents=True, exist_ok=False)
        target = target_dir / name
        size = 0
        try:
            with target.open("wb") as handle:
                while chunk := await upload.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise ApiError(
                            413,
                            "attachment_too_large",
                            "Attachment exceeds the server limit.",
                        )
                    handle.write(chunk)
        except BaseException:
            target.unlink(missing_ok=True)
            try:
                target_dir.rmdir()
            except OSError:
                pass
            raise
        expires = datetime.now(UTC) + UPLOAD_TTL
        record = StoredUpload(
            id=upload_id,
            session_id=session_id,
            name=name,
            mime=mime,
            size=size,
            path=str(target.resolve()),
            expires_at=expires.isoformat(),
        )
        try:
            (target_dir / "metadata.json").write_text(
                record.model_dump_json(indent=2),
                encoding="utf-8",
            )
        except BaseException:
            shutil.rmtree(target_dir, ignore_errors=True)
            raise
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
        path = Path(record.path)
        if not path.is_file():
            raise ApiError(404, "upload_not_found", "Upload was not found.")
        return path

    def pin(
        self,
        uri: str,
        *,
        session_id: str,
        name: str,
        mime: str,
    ) -> None:
        """Make one referenced upload durable for the lifetime of session state."""
        self.resolve(uri, session_id=session_id, name=name, mime=mime)
        record = self._load_uri(uri)
        changed = False
        if record.session_id is None:
            record.session_id = session_id
            changed = True
        if not record.durable:
            record.durable = True
            changed = True
        if not changed:
            return
        self._metadata_path(record.id).write_text(
            record.model_dump_json(indent=2),
            encoding="utf-8",
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
        if not upload_id.startswith("upl_") or "/" in upload_id or ".." in upload_id:
            raise ApiError(400, "invalid_attachment_uri", "Invalid upload URI.")
        metadata = self._metadata_path(upload_id)
        try:
            record = StoredUpload.model_validate_json(metadata.read_text("utf-8"))
        except (OSError, ValueError) as exc:
            raise ApiError(404, "upload_not_found", "Upload was not found.") from exc
        return record

    def gc_expired_orphans(self) -> int:
        """Remove expired uploads that were never referenced by durable state."""
        if not self.directory.is_dir():
            return 0
        removed = 0
        for child in self.directory.iterdir():
            if not child.is_dir() or not child.name.startswith("upl_"):
                continue
            try:
                record = StoredUpload.model_validate_json(
                    (child / "metadata.json").read_text("utf-8")
                )
            except (OSError, ValueError):
                continue
            if record.durable or not _expired(record.expires_at):
                continue
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
        return removed

    def _metadata_path(self, upload_id: str) -> Path:
        return self.directory / upload_id / "metadata.json"

    def _delete(self, upload_id: str) -> None:
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
