"""SQL topology and pagination. Payloads are fetched only for selected ids."""

from __future__ import annotations

from yoke.agent.models import ConversationEntry
from yoke.http.errors import ApiError
from yoke.session import SessionStore
from yoke.session.repository.models import SessionIndexEntry
from yoke.session.repository.rows import SESSION_SELECT, index_entry_from_row
from yoke.session.repository.utils import normalize_root

# UNION, rather than UNION ALL, also bounds corrupt cycles without a depth cap.
_PATH = """WITH RECURSIVE path(id, parent_id, kind, seq) AS (
    SELECT id, parent_id, kind, seq FROM entries
    WHERE session_id = :session AND id = :leaf
    UNION
    SELECT e.id, e.parent_id, e.kind, e.seq FROM entries e JOIN path p
    ON e.id = p.parent_id WHERE e.session_id = :session
) """


class SessionQueries:
    """Own bounded SQL selection independently of HTTP projection and runtime."""

    def __init__(self, store: SessionStore) -> None:
        self.store = store

    def path(self, session_id: str, leaf_id: str | None) -> list[tuple[str, str]]:
        with self.store.database.read() as connection:
            return connection.execute(
                _PATH + "SELECT id, kind FROM path ORDER BY seq",
                {"session": session_id, "leaf": leaf_id},
            ).fetchall()

    def page_ids(
        self,
        session_id: str,
        leaf_id: str | None,
        *,
        limit: int,
        anchor_id: str | None,
        order: str,
        tree: bool = False,
    ) -> tuple[list[str], bool]:
        with self.store.database.read() as connection:
            parameters: dict[str, object] = {
                "session": session_id,
                "leaf": leaf_id,
                "limit": limit + 1,
                "anchor": anchor_id,
            }
            prefix = "" if tree else _PATH
            source = "entries" if tree else "path"
            where = (
                "session_id = :session"
                if tree
                else "kind NOT IN ('instruction', 'memory_snapshot', 'tool_context')"
            )
            if anchor_id is not None:
                anchor = connection.execute(
                    prefix + f"SELECT seq FROM {source} WHERE {where} AND id = :anchor",
                    parameters,
                ).fetchone()
                if anchor is None:
                    raise ApiError(
                        400, "invalid_cursor_anchor", "Cursor anchor no longer exists."
                    )
                parameters["boundary"] = anchor[0]
                where += " AND seq " + ("<" if order == "desc" else ">") + " :boundary"
            direction = "DESC" if order == "desc" else "ASC"
            rows = connection.execute(
                prefix
                + f"SELECT id FROM {source} WHERE {where} ORDER BY seq {direction} LIMIT :limit",
                parameters,
            ).fetchall()
            return [row[0] for row in rows[:limit]], len(rows) > limit

    def children(self, session_id: str, ids: list[str]) -> dict[str, int]:
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        with self.store.database.read() as connection:
            return dict(
                connection.execute(
                    f"SELECT parent_id, count(*) FROM entries WHERE session_id = ? AND parent_id IN ({marks}) GROUP BY parent_id",
                    (session_id, *ids),
                ).fetchall()
            )

    def read(self, session_id: str, ids: list[str]) -> list[ConversationEntry] | None:
        with self.store.database.read() as connection:
            entries = self.store.read_entries(session_id, ids)
            if not entries:
                return entries
            private = set()
            for start in range(0, len(ids), 500):
                chunk = ids[start : start + 500]
                marks = ",".join("?" for _ in chunk)
                private.update(
                    row[0]
                    for row in connection.execute(
                        f"SELECT id FROM entries WHERE session_id = ? AND kind = 'tool_context' AND id IN ({marks})",
                        (session_id, *chunk),
                    )
                )
            for entry in entries:
                if entry.id in private:
                    entry.kind = "tool_context"
            return entries

    def sessions(
        self,
        *,
        directory: str | None,
        search: str | None,
        pinned: bool | None,
        archived: bool | None,
        order: str,
        limit: int,
        anchor_id: str | None,
    ) -> tuple[list[SessionIndexEntry], int, bool]:
        """Filter and page catalog metadata without selecting entry payloads."""
        where = []
        parameters: dict[str, object] = {"limit": limit + 1}
        root = normalize_root(directory)
        if root:
            where.append("workspace = :workspace")
            parameters["workspace"] = root
        if search:
            where.append(
                "(instr(yoke_casefold(id), :search) > 0 OR instr(yoke_casefold(coalesce(title, '')), :search) > 0 OR instr(yoke_casefold(coalesce(workspace, '')), :search) > 0)"
            )
            parameters["search"] = search.casefold()
        if pinned is not None:
            where.append("pinned = :pinned")
            parameters["pinned"] = int(pinned)
        if archived is not None:
            where.append("archived_at IS " + ("NOT NULL" if archived else "NULL"))
        condition = " WHERE " + " AND ".join(where) if where else ""
        column = (
            "created_at"
            if order.startswith("created")
            else "last_user_at"
            if order.startswith("lastUser")
            else "updated_at"
        )
        key = f"coalesce(nullif({column}, ''), created_at, '')"
        prefix = (
            f"WITH catalog AS (SELECT *, {key} AS sort_value FROM sessions{condition}) "
        )
        with self.store.database.read() as connection:
            total = connection.execute(
                prefix + "SELECT count(*) FROM catalog", parameters
            ).fetchone()[0]
            boundary = ""
            if anchor_id is not None:
                parameters["anchor"] = anchor_id
                anchor = connection.execute(
                    prefix + "SELECT sort_value FROM catalog WHERE id = :anchor",
                    parameters,
                ).fetchone()
                if anchor is None:
                    raise ApiError(
                        400, "invalid_cursor_anchor", "Cursor anchor no longer exists."
                    )
                parameters["sort_anchor"] = anchor[0]
                comparison = "<" if order.endswith("Desc") else ">"
                boundary = (
                    f" WHERE (sort_value, id) {comparison} (:sort_anchor, :anchor)"
                )
            direction = "DESC" if order.endswith("Desc") else "ASC"
            select = SESSION_SELECT.replace("FROM sessions", "FROM catalog")
            rows = connection.execute(
                prefix
                + select
                + boundary
                + f" ORDER BY sort_value {direction}, id {direction} LIMIT :limit",
                parameters,
            ).fetchall()
            return (
                [index_entry_from_row(row) for row in rows[:limit]],
                total,
                len(rows) > limit,
            )
