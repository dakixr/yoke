"""A populated v5 fixture upgrades only through the backed-up owner path."""

import json
import hashlib
import sqlite3

import pytest

from yoke.storage.database import Database, DatabaseError, migrations
from yoke.session.admissions import AdmissionRecord


@pytest.fixture
def v5_database(tmp_path):
    path = tmp_path / "v5.db"
    rows = [
        ("queued", "admitted", False, None, 0, False),
        ("paused", "admitted", False, None, 1, True),
        ("promoted", "promoted", False, None, None, False),
        ("settled", "promoted", True, "failed", None, False),
        ("removed", "removed", False, None, None, False),
    ]
    with sqlite3.connect(path) as connection:
        connection.create_function(
            "yoke_sha256", 1, lambda value: hashlib.sha256(value).hexdigest()
        )
        for _, sql in migrations()[:5]:
            connection.executescript(sql)
        connection.execute("PRAGMA user_version=5")
        connection.execute("INSERT INTO sessions(id) VALUES ('s')")
        connection.execute(
            "INSERT INTO session_queues(session_id,queue_rev) VALUES ('s',7)"
        )
        for input_id, state, settled, outcome, position, paused in rows:
            admission = dict(
                id=input_id,
                session_id="s",
                prompt=input_id,
                delivery="queue",
                fingerprint="f",
                time_created="then",
                admitted_seq=1,
                promoted_seq=3 if state == "promoted" else None,
                state=state,
                settled=settled,
                outcome=outcome,
                settled_at="now" if settled else None,
                queue_position=position,
            )
            pending = (
                json.dumps(
                    dict(id=input_id, prompt=input_id, created_at="then", paused=paused)
                )
                if position is not None
                else None
            )
            connection.execute(
                "INSERT INTO inputs(session_id,id,state,paused,position,prompt,admission,pending) VALUES ('s',?,?,?,?,?,?,?)",
                (
                    input_id,
                    state,
                    paused,
                    position,
                    input_id,
                    json.dumps(admission),
                    pending,
                ),
            )
        connection.execute(
            "INSERT INTO inputs(session_id,id,position,prompt,pending) VALUES ('s','native',2,'native',?)",
            (
                json.dumps(
                    dict(id="native", prompt="native", created_at="then", kind="queued")
                ),
            ),
        )
        blocked = dict(
            session_id="s",
            delivery="queue",
            fingerprint="fixture",
            time_created="then",
            admitted_seq=1,
            id="workspace",
            prompt="workspace",
            state="promoted",
            settled=False,
            outcome=None,
            settled_at=None,
            workspace_blocked=True,
            queue_position=1,
        )
        connection.execute(
            "INSERT INTO inputs(session_id,id,state,prompt,admission) VALUES ('s','workspace','promoted','workspace',?)",
            (json.dumps(blocked),),
        )
    return path


def test_v5_backfill_backup_and_readers_do_not_migrate(v5_database):
    before = v5_database.read_bytes()
    with pytest.raises(
        DatabaseError, match="Start the Yoke daemon or run yoke db migrate"
    ):
        Database(v5_database)
    assert v5_database.read_bytes() == before
    assert not list(v5_database.parent.glob("*.pre-v*"))
    database = Database(v5_database, migrate=True)
    with database.read() as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (6,)
        rows = connection.execute(
            "SELECT id,phase,admission FROM inputs ORDER BY id"
        ).fetchall()
        assert {item[0]: item[1] for item in rows} == {
            "queued": "queued",
            "paused": "paused",
            "promoted": "promoted",
            "settled": "settled",
            "removed": "removed",
            "native": "queued",
            "workspace": "paused",
        }
        for _, phase, admission in rows:
            record = AdmissionRecord.model_validate_json(admission)
            assert record.phase.kind == phase
            assert record.settled == (record.outcome is not None)
            assert record.model_dump()["state"] in {"admitted", "promoted", "removed"}
        queued = connection.execute(
            "SELECT id,position FROM inputs WHERE position IS NOT NULL ORDER BY position"
        ).fetchall()
        assert queued == [("queued", 0), ("workspace", 1), ("paused", 2), ("native", 3)]
        blocked_record = AdmissionRecord.model_validate_json(
            next(raw for key, _, raw in rows if key == "workspace")
        )
        assert blocked_record.workspace_blocked
        assert blocked_record.state == "admitted"
        assert connection.execute(
            "SELECT queue_rev,held_reason,input_revision FROM session_queues"
        ).fetchone() == (7, None, 0)
        assert "inputs_by_phase" in {
            row[1] for row in connection.execute("PRAGMA index_list(inputs)")
        }
    database.close()
    with sqlite3.connect(v5_database.with_name("v5.db.pre-v6")) as backup:
        assert backup.execute("PRAGMA user_version").fetchone() == (5,)
        assert backup.execute("SELECT count(*) FROM inputs").fetchone() == (7,)
        assert "phase" not in {
            row[1] for row in backup.execute("PRAGMA table_info(inputs)")
        }
