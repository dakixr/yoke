"""Apply tree intents once, independent of persistence representation."""

from __future__ import annotations

from yoke.http.errors import ApiError
from yoke.http.models.session import (
    TreeEntryPatchData,
    TreeEntryPatchResponse,
    TreeNavigateData,
    TreeNavigateRequest,
    TreeNavigateResponse,
)
from yoke.http.services.session_catalog import SessionCatalog
from yoke.http.reads.cursors import revision_from_timestamp
from yoke.http.reads.model import SessionReadModel
from yoke.session import SessionRecord


class SessionTreeMutations:
    """Persist and publish canonical navigation and label mutations."""

    def __init__(self, read_model: SessionReadModel, catalog: SessionCatalog) -> None:
        self.read_model = read_model
        self.catalog = catalog

    def navigate_tree(
        self, session_id: str, request: TreeNavigateRequest
    ) -> TreeNavigateResponse:
        self.require_revision(
            self.read_model.summary(session_id), request.expected_revision
        )
        edit = self.read_model.edit(session_id, request.target_id)
        self.require_revision(edit.record, request.expected_revision)
        updated, outcome = edit.navigate(request.branch_summary)
        revision = revision_from_timestamp(updated.updated_at)
        if updated is not edit.record:
            self.catalog.publish(
                updated,
                "session.tree.updated",
                {
                    "revision": revision,
                    "leafID": updated.leaf_id,
                    "targetID": request.target_id,
                },
            )
        return TreeNavigateResponse(
            data=TreeNavigateData(
                revision=revision,
                leaf_id=updated.leaf_id,
                editor_text=outcome.editor_text,
                summary_added=outcome.summary_appended,
            )
        )

    def set_tree_label(
        self,
        session_id: str,
        entry_id: str,
        *,
        expected_revision: int,
        label: str | None,
    ) -> TreeEntryPatchResponse:
        self.require_revision(self.read_model.summary(session_id), expected_revision)
        edit = self.read_model.edit(session_id, entry_id, label=True)
        self.require_revision(edit.record, expected_revision)
        updated, entry = edit.label(label)
        revision = revision_from_timestamp(updated.updated_at)
        self.catalog.publish(
            updated, "session.tree.updated", {"revision": revision, "entryID": entry_id}
        )
        return TreeEntryPatchResponse(
            data=TreeEntryPatchData(revision=revision, entry=entry)
        )

    @staticmethod
    def require_revision(record: SessionRecord, expected: int) -> None:
        actual = revision_from_timestamp(record.updated_at)
        if actual != expected:
            raise ApiError(
                409,
                "tree_revision_conflict",
                f"Tree changed since revision {expected}.",
                {"expectedRevision": expected, "actualRevision": actual},
            )
