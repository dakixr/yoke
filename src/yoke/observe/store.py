"""Local durable storage for Yoke Observe events."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from collections.abc import Iterable
from pathlib import Path

from yoke.observe.models import ObserveEvent
from yoke.observe.models import RunManifest
from yoke.observe.models import WorkflowState
from yoke.observe.projection import project_events
from pydantic import ValidationError


def default_observe_root(root: str | Path | None = None) -> Path:
    """Return the default local observe storage directory."""
    base = Path.cwd() if root is None else Path(root)
    return base.resolve() / ".yoke" / "observe"


def validate_run_id(run_id: str) -> str:
    """Return one path-safe Observe run identifier."""
    normalized = run_id.strip()
    if (
        not normalized
        or normalized in {".", ".."}
        or "/" in normalized
        or "\\" in normalized
    ):
        raise ValueError("run_id must be one non-empty path-safe name")
    return normalized


class _EventCursor:
    """Last validated position in an event log."""

    def __init__(self, file_id: tuple[int, int]) -> None:
        self.file_id = file_id
        self.sequence = 0
        self.offset = 0


class JsonlObserveStore:
    """Append-only JSONL Observe store under `.yoke/observe`."""

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = default_observe_root(root)
        self.runs_dir = self.root / "runs"
        self._event_cursors: dict[str, _EventCursor] = {}
        self._event_cursor_lock = threading.RLock()

    def create_run(self, manifest: RunManifest) -> None:
        """Create storage files for a run."""
        run_dir = self.run_dir(manifest.run_id)
        if (
            self.manifest_path(manifest.run_id).exists()
            or self.events_path(manifest.run_id).exists()
        ):
            raise ValueError(f"Observe run already exists: {manifest.run_id}")
        (run_dir / "artifacts").mkdir(parents=True, exist_ok=True)
        self._write_manifest(manifest)
        events_path = self.events_path(manifest.run_id)
        events_path.touch(exist_ok=True)

    def append(self, event: ObserveEvent) -> None:
        """Append one event and update the run manifest."""
        run_dir = self.run_dir(event.run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        with self.events_path(event.run_id).open("a", encoding="utf-8") as handle:
            handle.write(event.model_dump_json(exclude_none=True))
            handle.write("\n")
        manifest = self.manifest(event.run_id)
        if manifest is None:
            manifest = RunManifest(run_id=event.run_id, name=event.run_id)
        manifest.event_count = max(manifest.event_count, event.sequence)
        manifest.updated_at = event.timestamp
        if event.type == "workflow_completed":
            manifest.status = "completed"
        elif event.type == "workflow_failed":
            manifest.status = "failed"
        self._write_manifest(manifest)

    def events(
        self,
        run_id: str,
        *,
        after: int = 0,
    ) -> Iterable[ObserveEvent]:
        """Yield events for a run after an optional sequence number."""
        return iter(self._read_events(run_id, after=after))

    def _read_events(self, run_id: str, *, after: int) -> list[ObserveEvent]:
        path = self.events_path(run_id)
        try:
            handle = path.open("rb")
        except FileNotFoundError:
            return []
        with handle, self._event_cursor_lock:
            stat = os.fstat(handle.fileno())
            file_id = (stat.st_dev, stat.st_ino)
            cursor = self._event_cursors.get(run_id)
            if (
                cursor is None
                or cursor.file_id != file_id
                or stat.st_size < cursor.offset
            ):
                cursor = _EventCursor(file_id)
                self._event_cursors[run_id] = cursor

            use_cursor = after >= cursor.sequence
            start_offset = cursor.offset if use_cursor else 0
            latest_sequence = cursor.sequence if use_cursor else 0
            latest_offset = start_offset
            handle.seek(start_offset)
            events: list[ObserveEvent] = []
            while True:
                line_start = handle.tell()
                line = handle.readline()
                if not line:
                    break
                line_end = handle.tell()
                if not line.strip():
                    latest_offset = line_end
                    continue
                try:
                    event = ObserveEvent.model_validate_json(line)
                except ValidationError:
                    if (
                        not line.endswith(b"\n")
                        and line_end == os.fstat(handle.fileno()).st_size
                    ):
                        latest_offset = line_start
                        break
                    raise
                if event.sequence > after:
                    events.append(event)
                if event.sequence >= latest_sequence:
                    latest_sequence = event.sequence
                    latest_offset = line_end

            if latest_sequence >= cursor.sequence:
                cursor.sequence = latest_sequence
                cursor.offset = latest_offset
            return events

    def latest_state(self, run_id: str) -> WorkflowState | None:
        """Return the projected state for a run."""
        return project_events(self.events(run_id))

    def list_runs(self) -> list[RunManifest]:
        """Return known runs sorted by newest update time first."""
        if not self.runs_dir.is_dir():
            return []
        manifests = [
            manifest
            for path in self.runs_dir.iterdir()
            if path.is_dir()
            for manifest in [self.manifest(path.name)]
            if manifest is not None
        ]
        return sorted(manifests, key=lambda item: item.updated_at, reverse=True)

    def latest_run_id(self) -> str | None:
        """Return the most recently updated run id."""
        runs = self.list_runs()
        return runs[0].run_id if runs else None

    def run_dir(self, run_id: str) -> Path:
        """Return the directory for one run."""
        return self.runs_dir / validate_run_id(run_id)

    def events_path(self, run_id: str) -> Path:
        """Return the event log path for one run."""
        return self.run_dir(run_id) / "events.jsonl"

    def manifest_path(self, run_id: str) -> Path:
        """Return the manifest path for one run."""
        return self.run_dir(run_id) / "manifest.json"

    def manifest(self, run_id: str) -> RunManifest | None:
        """Load one run manifest if present."""
        path = self.manifest_path(run_id)
        if not path.is_file():
            return None
        try:
            return RunManifest.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError):
            return None

    def _write_manifest(self, manifest: RunManifest) -> None:
        path = self.manifest_path(manifest.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(manifest.model_dump(mode="json"), indent=2) + "\n"
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            dir=path.parent,
            text=True,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
