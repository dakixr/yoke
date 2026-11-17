"""SQL topology and pagination. Payloads are fetched only for selected ids."""

from __future__ import annotations

from yoke.agent.models import ConversationEntry
from yoke.http.errors import ApiError
from yoke.session import SessionStore
from yoke.http.reads.catalog import SessionCatalogQueries

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
        self.sessions = SessionCatalogQueries(store.database).sessions

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
