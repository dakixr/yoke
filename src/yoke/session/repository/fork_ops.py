from __future__ import annotations

from pathlib import Path
from typing import Any

from yoke.session.repository.models import SessionRecord
from yoke.session.repository.utils import fork_session_title
from yoke.session.repository.utils import normalize_root
from yoke.session.repository.utils import normalize_title
from yoke.session.repository.utils import new_unique_session_id
from yoke.session.repository.utils import timestamp
from yoke.session.repository.writer import clone_canonical_session

from yoke.session.repository.index import SESSION_INDEX_SUMMARY_VERSION


class _ForkOperations:
    def fork(
        self: Any,
        source_session_id: str,
        *,
        new_session_id_value: str | None = None,
        root: Path | str | None = None,
        title: str | None = None,
        selected_leaf_id: str | None = None,
        materialize_result: bool = True,
    ) -> SessionRecord:
        """Create a persisted copy of an existing session under a new id."""
        fork_id = new_session_id_value or new_unique_session_id(self.exists)
        if self.exists(fork_id):
            raise ValueError(f"Session already exists: {fork_id}")
        source_index = self.index_entry(source_session_id)
        source_signature = self.session_file_signature(source_session_id)
        if (
            source_index is not None
            and source_index.summary_version == SESSION_INDEX_SUMMARY_VERSION
            and source_index.entry_count is not None
            and source_signature is not None
            and source_index.file_size == source_signature[0]
            and source_index.file_mtime_ns == source_signature[1]
        ):
            now = timestamp()
            fork_root = normalize_root(root) or source_index.root
            fork_title = normalize_title(title) or fork_session_title(
                source_index.title
            )
            fork_leaf = (
                selected_leaf_id
                if selected_leaf_id is not None
                else source_index.leaf_id
            )
            forked = source_index.to_record().model_copy(
                update={
                    "id": fork_id,
                    "created_at": now,
                    "updated_at": now,
                    "root": fork_root,
                    "title": fork_title,
                    "pinned": False,
                    "archived_at": None,
                    "context_usage": None,
                    "leaf_id": fork_leaf,
                }
            )
            if clone_canonical_session(
                self._session_path(source_session_id),
                self._session_path(fork_id),
                metadata_changes={
                    "id": fork_id,
                    "created_at": now,
                    "updated_at": now,
                    "root": fork_root,
                    "title": fork_title,
                    "pinned": False,
                    "archived_at": None,
                    "context_usage": None,
                    "leaf_id": fork_leaf,
                },
            ):
                self.update_index(forked, entry_count=source_index.entry_count)
                return self.load(fork_id) if materialize_result else forked

        source = self.load(source_session_id)
        if source.created_at is None and not source.conversation_entries:
            raise ValueError(f"Session not found: {source_session_id}")
        now = timestamp()
        forked = source.model_copy(
            deep=True,
            update={
                "id": fork_id,
                "created_at": now,
                "updated_at": now,
                "root": normalize_root(root) or source.root,
                "title": normalize_title(title) or fork_session_title(source.title),
                "pinned": False,
                "archived_at": None,
                "context_usage": None,
                "leaf_id": (
                    selected_leaf_id if selected_leaf_id is not None else source.leaf_id
                ),
            },
        )
        self._write_session_record(forked)
        self.update_index(forked)
        return forked
