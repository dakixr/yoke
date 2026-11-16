"""Paginated tree presentation and revision-checked navigation over HTTP."""

from __future__ import annotations

from yoke.agent.models import ConversationEntry
from yoke.cli.backend.connection import SessionConnection
from yoke.cli.runtime.tree_view import TreeNode, build_tree_nodes
from yoke.http.models.session import (
    TreeNavigateData,
    TreeNavigateResponse,
    TreeResponse,
)


class DaemonTree:
    def __init__(self, connection: SessionConnection) -> None:
        self.connection = connection

    def roots(self) -> list[TreeNode]:
        entries: list[ConversationEntry] = []
        active: set[str] = set()
        cursor = None
        while True:
            params: dict[str, str | int] = {"limit": 200}
            if cursor:
                params["cursor"] = cursor
            data = TreeResponse.model_validate(
                self.connection.session_request("GET", "/tree", params=params)
            ).data
            for item in data.entries:
                entries.append(
                    ConversationEntry.model_validate(
                        {
                            "id": item.id,
                            "parent_id": item.parent_id,
                            "kind": item.kind,
                            "created_at": item.created_at,
                            "metadata": {"label": item.label},
                            "message": {
                                "role": "user" if item.kind == "user" else "assistant",
                                "content": item.preview,
                                "phase": item.phase,
                            },
                        }
                    )
                )
                if item.active:
                    active.add(item.id)
            cursor = data.cursor.next
            if not cursor:
                return build_tree_nodes(entries, active)

    def label(self, entry_id: str, label: str) -> None:
        tree = TreeResponse.model_validate(
            self.connection.session_request("GET", "/tree")
        ).data
        self.connection.session_request(
            "PATCH",
            f"/tree/{entry_id}",
            json={"expectedRevision": tree.revision, "label": label},
        )

    def navigate(
        self, entry_id: str, *, branch_summary: str | None
    ) -> TreeNavigateData:
        tree = TreeResponse.model_validate(
            self.connection.session_request("GET", "/tree")
        ).data
        return TreeNavigateResponse.model_validate(
            self.connection.session_request(
                "POST",
                "/tree/navigate",
                json={
                    "expectedRevision": tree.revision,
                    "targetID": entry_id,
                    "branchSummary": branch_summary,
                },
            )
        ).data
