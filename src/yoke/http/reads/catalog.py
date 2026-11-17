"""Session catalog filtering, relevance and cursor paging in one read snapshot."""

from __future__ import annotations

from yoke.http.errors import ApiError
from yoke.session.repository.models import SessionIndexEntry
from yoke.session.repository.rows import SESSION_SELECT, index_entry_from_row
from yoke.session.repository.utils import normalize_root
from yoke.storage.database import Database
from yoke.storage.search import MATCHES, literal_query, short_snippet


class SessionCatalogQueries:
    """Select list cards without loading transcript blobs, including for search."""

    def __init__(self, database: Database) -> None:
        self.database = database

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
        origin: str | None = None,
    ) -> tuple[list[SessionIndexEntry], int, bool]:
        where = []
        parameters: dict[str, object] = {"limit": limit + 1}
        root = normalize_root(directory)
        if root:
            where.append("workspace = :workspace")
            parameters["workspace"] = root
        searching = bool(search and search.strip())
        if searching:
            assert search is not None
            where.append(
                "(best.session_id IS NOT NULL OR instr(yoke_casefold(s.id), :search) > 0 "
                "OR instr(yoke_casefold(coalesce(workspace, '')), :search) > 0)"
            )
            parameters.update(
                search=search.strip().casefold(), fts=literal_query(search)
            )
        if pinned is not None:
            where.append("pinned = :pinned")
            parameters["pinned"] = int(pinned)
        if archived is not None:
            where.append("archived_at IS " + ("NOT NULL" if archived else "NULL"))
        if origin is not None:
            where.append("origin = :origin")
            parameters["origin"] = origin
        condition = " WHERE " + " AND ".join(where) if where else ""
        column = (
            "created_at"
            if order.startswith("created")
            else "last_user_at"
            if order.startswith("lastUser")
            else "updated_at"
        )
        key = f"coalesce(nullif({column}, ''), created_at, '')"
        source = (
            "sessions s LEFT JOIN best ON best.session_id = s.id"
            if searching
            else "sessions s"
        )
        extra = (
            "coalesce(best.score, 0) AS score, best.search_snippet"
            if searching
            else "0 AS score, NULL AS search_snippet"
        )
        prefix = (MATCHES if searching else "WITH ") + (
            f"catalog AS (SELECT s.*, {key} AS sort_value, {extra} FROM {source}{condition}) "
        )
        with self.database.read() as connection:
            total = connection.execute(
                prefix + "SELECT count(*) FROM catalog", parameters
            ).fetchone()[0]
            boundary = ""
            comparison = "<" if order.endswith("Desc") else ">"
            if anchor_id is not None:
                parameters["anchor"] = anchor_id
                anchor = connection.execute(
                    prefix + "SELECT score, sort_value FROM catalog WHERE id = :anchor",
                    parameters,
                ).fetchone()
                if anchor is None:
                    raise ApiError(
                        400, "invalid_cursor_anchor", "Cursor anchor no longer exists."
                    )
                parameters.update(score_anchor=anchor[0], sort_anchor=anchor[1])
                boundary = (
                    " WHERE score > :score_anchor OR (score = :score_anchor AND "
                    f"(sort_value, id) {comparison} (:sort_anchor, :anchor))"
                )
            direction = "DESC" if order.endswith("Desc") else "ASC"
            select = SESSION_SELECT.replace(
                "FROM sessions", ", search_snippet FROM catalog"
            )
            rows = connection.execute(
                prefix
                + select
                + boundary
                + f" ORDER BY score ASC, sort_value {direction}, id {direction} LIMIT :limit",
                parameters,
            ).fetchall()
            return (
                [
                    index_entry_from_row(row[:-1]).model_copy(
                        update={"search_snippet": short_snippet(row[-1])}
                    )
                    for row in rows[:limit]
                ],
                total,
                len(rows) > limit,
            )
