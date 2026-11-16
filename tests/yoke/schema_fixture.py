"""Copy a pristine schema for tests whose subject is above SQLite migration."""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import NamedTemporaryFile

import pytest

from yoke.storage.database import Database


@pytest.fixture(scope="session")
def empty_schema(tmp_path_factory: pytest.TempPathFactory) -> bytes:
    path = tmp_path_factory.mktemp("schema") / "empty.db"
    database = Database(path)
    database.close()
    return path.read_bytes()


@pytest.fixture(autouse=True)
def fresh_database_from_schema(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    empty_schema: bytes,
) -> None:
    # Storage, legacy import and atomic-write tests exercise first-open and
    # migration behaviour themselves. Existing databases always migrate normally.
    path = request.node.path
    if "storage" in path.parts or path.name in {
        "test_session_atomic_writes.py",
        "test_session_format_fixtures.py",
    }:
        return
    initialize = Database.__init__

    def initialize_fresh(database: Database, path: Path) -> None:
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(dir=path.parent) as source:
                source.write(empty_schema)
                source.flush()
                try:
                    # Publish a complete, independent inode without replacing a
                    # database another first-open caller has already created.
                    os.link(source.name, path)
                except FileExistsError:
                    pass
        initialize(database, path)

    monkeypatch.setattr(Database, "__init__", initialize_fresh)
